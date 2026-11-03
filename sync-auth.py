#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync-auth.py —— 把 Windows 上 WorkBuddy 的登录凭据解密后同步到局域网 Linux 主机。

为什么需要它
------------
WorkBuddy 桌面端写出的 ``workbuddy-desktop.info`` 里，``accessToken`` 是
``$wbEncrypted`` 信封（AES-256-GCM，密钥来自客户端原生存储、与机器绑定）。
把文件原样拷到 Linux 是解不开的——Linux 上没有 WorkBuddy 运行时。

所以本脚本在 Windows 侧用原版 ``signin.py`` 的解密能力拿到明文 access token，
生成一份 Linux 可直接读取的最小凭据，通过 SSH 推到远端。远端的 ``signin.py``
只需 ``WORKBUDDY_AUTH_FILE`` 指向该文件即可，无需任何改动。

access token 有效期 30 天、refreshtoken 60 天，且由桌面端负责续期，
因此本脚本需要常驻定时（见 install-sync-windows.ps1）。

用法
----
    python sync-auth.py setup --host 192.168.1.100   # 新机器一键准备（生成密钥 + 写配置）
    python sync-auth.py doctor      # 只检查环境，不联网、不推送
    python sync-auth.py export      # 解密并写出本地 auth.export.json
    python sync-auth.py push        # 解密 + 推送到远端
    python sync-auth.py deploy      # 上传 signin.py + 安装 systemd + 推送凭据
    python sync-auth.py check       # 在远端跑一次只读状态查询
    python sync-auth.py silent-push # 供计划任务用：结果写 sync.log，不输出

配置优先级：命令行 > 环境变量 > sync-config.json > 内置默认值。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import signin  # noqa: E402  复用上游的凭据探测与解密，避免重复实现

CONFIG_FILE = os.path.join(HERE, "sync-config.json")
CACHE_FILE = os.path.join(HERE, ".sync-cache.json")
DEFAULT_EXPORT = os.path.join(HERE, "auth.export.json")
DEFAULT_LOG = os.path.join(HERE, "sync.log")

DEFAULTS = {
    "host": "192.168.1.100",
    "user": "root",
    "port": 22,
    "identity": "~/.ssh/workbuddy_debian",
    "remote_dir": "/opt/workbuddy-auto-signin",
    "remote_auth": "",          # 留空 = <remote_dir>/auth.json
    "remote_python": "python3",
    "ssh": "",                  # 留空 = 自动探测
    "expiry_warn_days": 3,
    "force_repush_days": 7,
}

# 推送时低于该剩余天数就告警：桌面端该刷新令牌了，否则远端会 401
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "PreferredAuthentications=publickey",
]


# --------------------------------------------------------------------------
# 配置与工具
# --------------------------------------------------------------------------

def _merge_config(args):
    cfg = dict(DEFAULTS)
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            if isinstance(file_cfg, dict):
                cfg.update({k: v for k, v in file_cfg.items() if v not in (None, "")})
        except (OSError, ValueError) as e:
            _warn("读取 %s 失败（%s），改用默认配置" % (CONFIG_FILE, type(e).__name__))
    env_map = {
        "SYNC_HOST": "host", "SYNC_USER": "user", "SYNC_PORT": "port",
        "SYNC_IDENTITY": "identity", "SYNC_REMOTE_DIR": "remote_dir",
        "SYNC_REMOTE_PYTHON": "remote_python", "SYNC_SSH": "ssh",
    }
    for env, key in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    for attr in ("host", "user", "port", "identity", "remote_dir", "remote_python", "ssh"):
        value = getattr(args, attr, None)
        if value not in (None, ""):
            cfg[attr] = value
    cfg["port"] = int(cfg["port"])
    cfg["remote_dir"] = cfg["remote_dir"].rstrip("/")
    if not cfg["remote_auth"]:
        cfg["remote_auth"] = cfg["remote_dir"] + "/auth.json"
    return cfg


def _identity_path(cfg):
    return os.path.abspath(os.path.expanduser(cfg["identity"]))


def find_ssh(explicit=None):
    """定位原生 ssh 客户端。

    Windows 上必须优先用系统 OpenSSH（System32\\OpenSSH\\ssh.exe）：
    Git/MSYS 版的 ssh 会对命令行里形似 POSIX 路径的参数做路径转换，
    把远端命令 ``mkdir -p /opt/x`` 改写成 ``D:/Git/opt/x``，远端直接失败。
    """
    if explicit:
        return explicit
    if os.name == "nt":
        native = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "OpenSSH", "ssh.exe")
        if os.path.isfile(native):
            return native
    found = shutil.which("ssh")
    if found:
        return found
    return None


def find_ssh_keygen(explicit=None):
    """定位 ssh-keygen：优先取与 ssh 同目录的那个，避免又混进 MSYS 版。"""
    if explicit:
        return explicit
    ssh = find_ssh()
    if ssh:
        name = "ssh-keygen.exe" if os.name == "nt" else "ssh-keygen"
        candidate = os.path.join(os.path.dirname(ssh), name)
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("ssh-keygen")


# 写进 sync-config.json 的字段（remote_auth 由 remote_dir 推导，不落盘）
SETUP_KEYS = ("host", "user", "port", "identity", "remote_dir", "remote_python",
              "expiry_warn_days", "force_repush_days")


def _read_config_file():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _config_for_write(cfg, existing=None):
    """把当前生效的配置写成可落盘的 dict，保留文件里已有的其它字段。"""
    data = dict(existing) if isinstance(existing, dict) else {}
    for key in SETUP_KEYS:
        value = cfg.get(key)
        if value not in (None, ""):
            data[key] = value
    return data


def _quote_local(path):
    """本地命令里的路径加引号（Windows 用双引号，POSIX 用单引号）。"""
    if os.name == "nt":
        return '"%s"' % path
    return "'%s'" % path.replace("'", "'\\''")


def _authorize_commands(cfg, identity):
    """生成把公钥装到远端的命令，供用户复制粘贴（需要一次密码登录）。

    这是唯一需要人动手的一步：私钥留在本机，公钥必须由用户亲手写进远端
    ``authorized_keys``——密码不该出现在脚本、配置或对话里。
    """
    target = "%s@%s" % (cfg["user"], cfg["host"])
    pub = identity + ".pub"
    port_opt = "" if int(cfg["port"]) == 22 else " -p %d" % int(cfg["port"])
    remote = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
              "cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys")

    # Windows 侧尽量用 $env:USERPROFILE：换一台电脑、换用户名都不用改命令
    home = os.path.expanduser("~")
    try:
        rel = os.path.relpath(pub, home)
    except ValueError:
        rel = None
    if rel and not rel.startswith("..") and not os.path.isabs(rel):
        win_pub = "$env:USERPROFILE\\" + rel.replace("/", "\\")
    else:
        win_pub = pub.replace("/", "\\")

    ssh_exe = '"$env:SystemRoot\\System32\\OpenSSH\\ssh.exe"'
    return {
        "powershell": 'type "%s" | & %s%s %s "%s"' % (win_pub, ssh_exe, port_opt, target, remote),
        "posix": "ssh-copy-id -i %s%s %s" % (_quote_local(pub), port_opt, target),
    }


def _ssh_base(cfg):
    ssh = find_ssh(cfg.get("ssh"))
    if not ssh:
        raise RuntimeError("未找到 ssh 客户端：请安装 OpenSSH Client，或用 --ssh 指定路径")
    argv = [ssh] + SSH_OPTS + ["-p", str(cfg["port"])]
    identity = _identity_path(cfg)
    if os.path.isfile(identity):
        argv += ["-i", identity]
    return argv


def ssh_run(cfg, remote_cmd, stdin_bytes=None, timeout=60):
    """执行远端命令，返回 (returncode, stdout, stderr) 文本。"""
    argv = _ssh_base(cfg) + ["%s@%s" % (cfg["user"], cfg["host"]), remote_cmd]
    try:
        proc = subprocess.run(argv, input=stdin_bytes, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "SSH 超时（%ss）" % timeout
    except OSError as e:
        return 125, "", "无法启动 ssh：%s" % e
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace").strip(),
            proc.stderr.decode("utf-8", "replace").strip())


def _warn(msg):
    sys.stderr.write("[warn] %s\n" % msg)


def _shell_quote(text):
    """远端路径只做单引号包裹；拒绝换行/单引号等会破坏命令的输入。"""
    if not isinstance(text, str) or not text or "\n" in text or "'" in text or "\x00" in text:
        raise ValueError("非法路径：%r" % (text,))
    return "'" + text + "'"


def _write_log(line):
    try:
        with open(DEFAULT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# 凭据处理
# --------------------------------------------------------------------------

def extract_token():
    """解密本机凭据，返回 (明文 token, 元信息 dict)。不打印、不落盘。"""
    auth_file, looked_in = signin.find_auth_file()
    if not auth_file or not os.path.exists(auth_file):
        raise SystemExit("未找到 WorkBuddy 登录凭据，已查找：\n  " + "\n  ".join(looked_in))
    session = signin.load_session_retry(auth_file)
    kind = signin._token_format(session["auth"]["accessToken"])
    resolved = signin.resolve_session(session)   # sym-v1 在这里由客户端运行时解密
    token = resolved["auth"]["accessToken"]
    auth = resolved.get("auth") or {}
    account = resolved.get("account") or {}
    meta = {
        "kind": kind,
        "source": auth_file,
        "domain": auth.get("domain") or "",
        "endpoint": auth.get("endpoint") or signin.DEFAULT_ENDPOINT,
        "uid": account.get("uid") or "",
        "enterpriseId": account.get("enterpriseId") or "",
        "expiresAt": auth.get("expiresAt"),
        "refreshExpiresAt": auth.get("refreshExpiresAt"),
    }
    return token, meta


def build_payload(token, meta):
    """最小可用凭据：signin.py 的 _validate_session 只认这几个字段。"""
    auth = {"accessToken": token, "endpoint": meta["endpoint"]}
    if meta["domain"]:
        auth["domain"] = meta["domain"]
    account = {"uid": meta["uid"]}
    if meta["enterpriseId"]:
        account["enterpriseId"] = meta["enterpriseId"]
    payload = {
        "auth": auth,
        "account": account,
        # 元信息：远端不读，仅供人工核对与告警
        "_sync": {
            "exportedAt": _now_iso(),
            "sourceHost": os.environ.get("COMPUTERNAME", "windows"),
            "credentialFormat": meta["kind"],
            "expiresAt": _ms_to_iso(meta["expiresAt"]),
        },
    }
    if meta["expiresAt"]:
        payload["_sync"]["expiresAtMs"] = meta["expiresAt"]
    return payload


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).astimezone() \
            .isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _ts_to_text(ts):
    """把 time.time() 时间戳转成本地可读文本；缺失时返回「未知」。"""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "未知"


def _token_fingerprint(token):
    # 只用于判断"密文有没有变"，不落盘 token 本身
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _credential_stamp():
    """当前凭据密文的指纹：同样的 envelope 说明桌面端还没刷新，无需重复解密。"""
    auth_file, _ = signin.find_auth_file()
    if not auth_file:
        return None
    try:
        with open(auth_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
        token = (raw.get("auth") or {}).get("accessToken")
        blob = json.dumps(token, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    except (OSError, ValueError, AttributeError):
        return None


def _read_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------

def cmd_export(cfg, out_path):
    token, meta = extract_token()
    payload = build_payload(token, meta)
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    target = os.path.abspath(os.path.expanduser(out_path or DEFAULT_EXPORT))
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    if os.name == "nt":
        _harden_windows_acl(target)
    days = _days_left(meta["expiresAt"])
    result = {
        "result": "EXPORTED", "path": target, "bytes": len(data),
        "credential_format": meta["kind"], "domain": meta["domain"],
        "account_uid": meta["uid"], "token_expires_at": _ms_to_iso(meta["expiresAt"]),
        "days_left": days, "needs_attention": days is not None and days < cfg["expiry_warn_days"],
        "report": "已导出明文凭据到 %s（%d 天后过期）" % (target, days if days is not None else -1),
    }
    return result, meta


def _harden_windows_acl(path):
    """Windows 上没有 chmod 的语义，用 icacls 只保留当前用户。"""
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:F" % user],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def _days_left(expires_at_ms):
    try:
        left = int(expires_at_ms) / 1000.0 - time.time()
        return int(left // 86400)
    except (TypeError, ValueError):
        return None


def cmd_push(cfg, force=False, quiet=False):
    """解密并推送。桌面端未刷新令牌时跳过，省掉一次客户端子进程。"""
    stamp = _credential_stamp()
    cache = _read_cache()
    last_push = cache.get("lastPushedAt", 0)
    age_days = (time.time() - last_push) / 86400.0 if last_push else 1e9
    same = stamp and stamp == cache.get("stamp")

    if same and not force and age_days < cfg["force_repush_days"]:
        return {"result": "SKIPPED", "report": "凭据未变化（上次推送 %s），跳过"
                % _ts_to_text(last_push), "needs_attention": False}, None

    token, meta = extract_token()
    payload = build_payload(token, meta)
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    remote_tmp = cfg["remote_auth"] + ".tmp"

    remote_dir = _shell_quote(cfg["remote_dir"])
    code, out, err = ssh_run(cfg, "mkdir -p %s && umask 077 && cat > %s"
                             % (remote_dir, _shell_quote(remote_tmp)), stdin_bytes=data, timeout=120)
    if code != 0:
        return _fail("写入远端临时文件失败", code, out, err, cfg), None

    code, out, err = ssh_run(cfg, "chmod 600 %s && mv -f %s %s && ls -l %s"
                             % (_shell_quote(remote_tmp), _shell_quote(remote_tmp),
                                _shell_quote(cfg["remote_auth"]), _shell_quote(cfg["remote_auth"])),
                             timeout=30)
    if code != 0:
        return _fail("替换远端凭据失败", code, out, err, cfg), None

    # 远端自检：确认 signin.py 能用这份凭据（只读 doctor，不联网）
    verify = verify_remote(cfg)
    if verify is not None and verify.get("result") not in ("AUTH_READY",):
        return {"result": "PUSHED_UNVERIFIED", "report": "凭据已推送，但远端自检未通过：%s"
                % verify.get("report", ""), "needs_attention": True}, meta

    cache.update({"stamp": stamp, "lastPushedAt": time.time(),
                  "fingerprint": _token_fingerprint(token), "host": cfg["host"]})
    _write_cache(cache)

    days = _days_left(meta["expiresAt"])
    attention = days is not None and days < cfg["expiry_warn_days"]
    report = "已同步到 %s@%s:%s（token %s 过期）" % (
        cfg["user"], cfg["host"], cfg["remote_auth"],
        "%d 天后" % days if days is not None else "时间未知")
    if attention:
        report += "；请尽快打开 WorkBuddy 桌面端续期"
    return {"result": "PUSHED", "report": report, "needs_attention": attention,
            "token_expires_at": _ms_to_iso(meta["expiresAt"]), "days_left": days,
            "remote_ls": out}, meta


def _fail(summary, code, out, err, cfg):
    detail = (err or out or "").splitlines()
    hint = ""
    if any(k in (err or "") for k in ("Permission denied", "Too many authentication")):
        hint = "（SSH 认证失败：公钥是否已装到 %s@%s？）" % (cfg["user"], cfg["host"])
    elif "Host key verification failed" in (err or ""):
        hint = "（主机密钥校验失败：删掉 known_hosts 中该主机条目后重试）"
    elif code == 124:
        hint = "（SSH 超时：检查 %s 是否在线）" % cfg["host"]
    elif "sudo" in (err or "") and ("terminal is required" in (err or "") or "password is required" in (err or "")):
        hint = "（安装需要 root：给 %s 配免密 sudo，或直接用 root 账号部署）" % cfg["user"]
    return {"result": "ERROR", "report": summary + hint + ("：" + detail[-1] if detail else ""),
            "needs_attention": True}


def verify_remote(cfg):
    """远端跑 doctor（离线自检），返回解析后的 JSON 或 None。"""
    remote = "%s %s doctor" % (cfg["remote_python"],
                              _shell_quote(cfg["remote_dir"] + "/signin.py"))
    env = "WORKBUDDY_AUTH_FILE=%s " % _shell_quote(cfg["remote_auth"])
    code, out, err = ssh_run(cfg, env + remote, timeout=60)
    line = _last_json_line(out)
    if line is not None:
        return line
    if err:
        _warn("远端自检输出：%s" % err.splitlines()[-1])
    return None


def cmd_check(cfg):
    remote = "%s %s status" % (cfg["remote_python"],
                              _shell_quote(cfg["remote_dir"] + "/signin.py"))
    env = "WORKBUDDY_AUTH_FILE=%s " % _shell_quote(cfg["remote_auth"])
    code, out, err = ssh_run(cfg, env + remote, timeout=180)
    body = _last_json_line(out)
    if body is None:
        return {"result": "ERROR", "report": "远端未返回 JSON：%s"
                % (err.splitlines()[-1] if err else "无输出"), "needs_attention": True}
    body["remote_exit_code"] = code
    return body


def _last_json_line(text):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def cmd_deploy(cfg):
    """上传 signin.py + install-debian.sh，注册 systemd 定时任务，再推送凭据。"""
    steps = []
    script = os.path.join(HERE, "signin.py")
    installer = os.path.join(HERE, "install-debian.sh")
    for path in (script, installer):
        if not os.path.isfile(path):
            return {"result": "ERROR", "report": "缺少文件：%s" % path, "needs_attention": True}, None

    code, out, err = ssh_run(cfg, "mkdir -p %s" % _shell_quote(cfg["remote_dir"]), timeout=30)
    if code != 0:
        return _fail("创建远端目录失败", code, out, err, cfg), None
    steps.append("目录就绪")

    for local, name in ((script, "signin.py"), (installer, "install-debian.sh")):
        with open(local, "rb") as f:
            data = f.read()
        code, out, err = ssh_run(
            cfg, "cat > %s" % _shell_quote(os.path.join(cfg["remote_dir"], name).replace("\\", "/")),
            stdin_bytes=data, timeout=120)
        if code != 0:
            return _fail("上传 %s 失败" % name, code, out, err, cfg), None
        steps.append("已上传 %s" % name)

    with open(installer, "rb") as f:
        installer_bytes = f.read()
    # 允许非 root 账号部署：能免密 sudo 就自动提权。ssh 是非交互的，
    # 所以必须用 sudo -n —— 需要密码时直接失败，而不是卡在等输入。
    remote_dir_q = _shell_quote(cfg["remote_dir"])
    install_cmd = ('if [ "$(id -u)" -eq 0 ]; then exec bash -s -- %s; '
                   'else exec sudo -n bash -s -- %s; fi' % (remote_dir_q, remote_dir_q))
    code, out, err = ssh_run(cfg, install_cmd, stdin_bytes=installer_bytes, timeout=180)
    if code != 0:
        return _fail("远端安装脚本执行失败", code, out, err, cfg), None
    steps.append("systemd 已注册")

    result, meta = cmd_push(cfg, force=True)
    if result.get("result") not in ("PUSHED",):
        result["report"] = "部署未完成：" + result.get("report", "")
        return result, meta
    steps.append("凭据已推送")

    check = cmd_check(cfg)
    ok = check.get("http") == 200 or check.get("step") == "status"
    return {
        "result": "DEPLOYED" if ok else "DEPLOYED_UNVERIFIED",
        "report": "部署完成：%s；远端状态检查 %s" % (
            " → ".join(steps), "HTTP %s" % check.get("http") if ok else check.get("report", "")),
        "needs_attention": not ok,
        "remote_install_output": out.splitlines()[-6:] if out else [],
        "status": check,
    }, meta


def cmd_setup(cfg):
    """新机器一键准备：验本机凭据 → 生成密钥 → 写配置 → 输出授权命令。

    刻意不联网授权、不猜密码：唯一的人工步骤（把公钥写进远端
    ``authorized_keys``）由用户自己粘贴执行。
    """
    target = "%s@%s:%d" % (cfg["user"], cfg["host"], cfg["port"])
    auth_file, looked = signin.find_auth_file()
    if not auth_file:
        return {
            "result": "ERROR",
            "report": "本机找不到 WorkBuddy 凭据，先在当前 Windows 上装好桌面端并登录一次"
                      "（或设 WORKBUDDY_AUTH_FILE 指向凭据文件）",
            "looked_in": looked,
            "needs_attention": True,
        }, None

    ssh = find_ssh(cfg.get("ssh"))
    if not ssh:
        return {
            "result": "ERROR",
            "report": "未找到 ssh 客户端：Windows「设置 → 可选功能 → 添加 OpenSSH 客户端」",
            "needs_attention": True,
        }, None

    identity = _identity_path(cfg)
    key_created = False
    if not os.path.isfile(identity):
        keygen = find_ssh_keygen()
        if not keygen:
            return {
                "result": "ERROR",
                "report": "缺少私钥，且没找到 ssh-keygen：%s" % identity,
                "needs_attention": True,
            }, None
        folder = os.path.dirname(identity)
        if folder:
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                return {"result": "ERROR", "report": "创建 %s 失败：%s" % (folder, e),
                        "needs_attention": True}, None
        # 注释里不写主机名：authorized_keys 会长期留在远端，没必要带上本机身份
        argv = [keygen, "-t", "ed25519", "-N", "", "-C", "workbuddy-auto-signin",
                "-f", identity, "-q"]
        try:
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"result": "ERROR", "report": "生成密钥失败：%s" % e,
                    "needs_attention": True}, None
        if proc.returncode != 0 or not os.path.isfile(identity):
            detail = proc.stderr.decode("utf-8", "replace").strip()
            return {"result": "ERROR",
                    "report": "生成密钥失败：%s" % (detail or "ssh-keygen 退出码 %s" % proc.returncode),
                    "needs_attention": True}, None
        key_created = True

    out = {
        "result": "OK",
        "target": target,
        "identity": identity,
        "key_created": key_created,
        "auth_file": auth_file,
        "ssh": ssh,
        "needs_attention": False,
    }

    try:
        with open(CONFIG_FILE, "w", encoding="utf-8", newline="\n") as f:
            json.dump(_config_for_write(cfg, _read_config_file()), f,
                      indent=2, ensure_ascii=False)
            f.write("\n")
        out["config_file"] = CONFIG_FILE
    except OSError as e:
        out.update(result="ERROR", needs_attention=True,
                   report="写入 %s 失败：%s" % (CONFIG_FILE, e))
        return out, None

    pub_path = identity + ".pub"
    try:
        with open(pub_path, "r", encoding="utf-8") as f:
            out["public_key"] = f.read().strip()
    except OSError:
        out["public_key"] = ""

    commands = _authorize_commands(cfg, identity)
    code, sout, _ = ssh_run(cfg, "echo CONNECTED", timeout=20)
    if code == 0 and "CONNECTED" in sout:
        out.update(result="SETUP_READY", ssh_ready=True,
                   report="配置已写入，SSH 免密已通（%s），直接部署" % target,
                   next_step="python sync-auth.py deploy")
    else:
        out.update(result="NEED_KEY_AUTH", ssh_ready=False,
                   authorize_commands=commands,
                   report="配置已写入，但公钥还没装到 %s：把 authorize_commands.powershell "
                          "粘到 PowerShell 跑一次（会提示输入一次远端密码），然后重新执行本命令"
                          % target,
                   next_step="python sync-auth.py setup（授权后重跑，或直接 deploy）")
    return out, None


def cmd_doctor(cfg):
    ssh = find_ssh(cfg.get("ssh"))
    identity = _identity_path(cfg)
    out = {
        "result": "OK",
        "ssh": ssh or "未找到",
        "identity": identity if os.path.isfile(identity) else "缺失：" + identity,
        "target": "%s@%s:%d" % (cfg["user"], cfg["host"], cfg["port"]),
        "remote_auth": cfg["remote_auth"],
        "python": sys.version.split()[0],
        "needs_attention": False,
    }
    problems = []
    if not ssh:
        problems.append("缺少 ssh 客户端")
    if not os.path.isfile(identity):
        problems.append("缺少私钥")
    if problems:
        out.update(result="ERROR", needs_attention=True, report="；".join(problems))
        return out, None
    code, sout, serr = ssh_run(cfg, "echo CONNECTED && test -f %s && echo AUTH_PRESENT || echo AUTH_MISSING"
                               % _shell_quote(cfg["remote_auth"]), timeout=25)
    out["ssh_exit"] = code
    out["ssh_output"] = sout
    if code == 0 and "CONNECTED" in sout:
        out["report"] = "SSH 连通，远端凭据%s" % ("已存在" if "AUTH_PRESENT" in sout else "尚未推送")
    else:
        out.update(result="ERROR", needs_attention=True,
                   report="SSH 不通：%s" % (serr.splitlines()[-1] if serr else "退出码 %s" % code))
    return out, None


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="同步 WorkBuddy 凭据到局域网 Linux 主机")
    parser.add_argument("action", nargs="?", default="push",
                        choices=["setup", "doctor", "export", "push", "deploy", "check",
                                 "silent-push"])
    parser.add_argument("--host"), parser.add_argument("--user"), parser.add_argument("--port")
    parser.add_argument("--identity"), parser.add_argument("--remote-dir")
    parser.add_argument("--remote-python"), parser.add_argument("--ssh")
    parser.add_argument("--out", help="export 的输出路径")
    parser.add_argument("--force", action="store_true", help="忽略缓存，强制重新解密推送")
    args = parser.parse_args(argv)

    silent = args.action == "silent-push"
    action = "push" if silent else args.action
    started = time.time()
    try:
        cfg = _merge_config(args)
        if action == "setup":
            result, _ = cmd_setup(cfg)
        elif action == "doctor":
            result, _ = cmd_doctor(cfg)
        elif action == "export":
            result, _ = cmd_export(cfg, args.out)
        elif action == "check":
            result = cmd_check(cfg)
        elif action == "deploy":
            result, _ = cmd_deploy(cfg)
        else:
            result, _ = cmd_push(cfg, force=args.force, quiet=silent)
    except SystemExit as e:
        result = {"result": "ERROR", "report": str(e), "needs_attention": True}
    except signin.AuthError as e:
        result = e.output()
    except Exception as e:  # 任何异常都要落盘，否则定时任务会静默失灵
        result = {"result": "ERROR", "report": "同步异常（%s: %s）" % (type(e).__name__, e),
                  "needs_attention": True}

    result.setdefault("elapsed", round(time.time() - started, 1))
    line = json.dumps(result, ensure_ascii=False)
    if silent:
        # 空跑不落盘，只在推送成功/需要处理时记录
        if result.get("result") not in ("SKIPPED",) or result.get("needs_attention"):
            _write_log("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line))
    else:
        print(line)
    return 0 if result.get("result") in ("OK", "EXPORTED", "PUSHED", "SKIPPED",
                                         "DEPLOYED", "AUTH_READY", "SETUP_READY") else 1


if __name__ == "__main__":
    sys.exit(main())
