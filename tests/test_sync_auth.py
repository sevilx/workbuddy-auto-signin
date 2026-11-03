"""sync-auth.py 的契约测试。

重点不是覆盖率，而是锁住两条硬约束：
1. 生成的明文凭据必须仍被 signin.py 接受（上游改 _validate_session 时立刻暴露）
2. 明文里不能夹带 refreshToken 之类不需要的凭据
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import signin  # noqa: E402


def _load_sync_auth():
    spec = importlib.util.spec_from_file_location("sync_auth", ROOT / "sync-auth.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync_auth = _load_sync_auth()

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"


def _meta(**over):
    meta = {
        "kind": "sym-v1",
        "source": r"C:\fake\workbuddy-desktop.info",
        "domain": "www.workbuddy.cn",
        "endpoint": signin.DEFAULT_ENDPOINT,
        "uid": "f0e33edd-f57b-4022-ab3a-7ecd8e98e798",
        "enterpriseId": "",
        "expiresAt": 1796262562984,
        "refreshExpiresAt": 1798854561984,
    }
    meta.update(over)
    return meta


class TestPayloadContract:
    def test_payload_accepted_by_signin(self):
        """最小凭据必须能通过 signin.py 的结构校验。"""
        payload = sync_auth.build_payload(TOKEN, _meta())
        signin._validate_session(payload)   # 不抛异常即通过

    def test_payload_builds_headers(self):
        """校验通过还不够，还得能拼出请求头。"""
        payload = sync_auth.build_payload(TOKEN, _meta())
        headers = signin.build_headers(payload)
        assert headers["Authorization"] == "Bearer " + TOKEN
        assert headers["X-User-Id"] == "f0e33edd-f57b-4022-ab3a-7ecd8e98e798"
        assert headers["X-Domain"] == "www.workbuddy.cn"

    def test_enterprise_headers_only_when_present(self):
        without = signin.build_headers(sync_auth.build_payload(TOKEN, _meta()))
        assert "X-Enterprise-Id" not in without
        with_ent = sync_auth.build_payload(TOKEN, _meta(enterpriseId="ent-123"))
        assert signin.build_headers(with_ent)["X-Enterprise-Id"] == "ent-123"

    def test_no_refresh_token_leaks(self):
        """明文里只放 access token，绝不带 refresh token。"""
        raw = json.dumps(sync_auth.build_payload(TOKEN, _meta()), ensure_ascii=False)
        assert "refreshToken" not in raw
        assert "refresh" not in raw.lower()

    def test_export_is_valid_json_and_minimal(self):
        payload = sync_auth.build_payload(TOKEN, _meta())
        assert set(payload) == {"auth", "account", "_sync"}
        assert set(payload["auth"]) == {"accessToken", "endpoint", "domain"}
        assert set(payload["account"]) == {"uid"}
        # 能被 signin.load_session 等价地读回来
        assert json.loads(json.dumps(payload))["auth"]["accessToken"] == TOKEN


class TestTokenFormatGuards:
    @pytest.mark.parametrize("bad", ["has space", "new\nline", "中文", "a" * 32769])
    def test_invalid_token_rejected_by_signin(self, bad):
        """脚本不自己发明校验，非法 token 交给上游判定。

        注意分工：_validate_session 只管字段是否齐全，token 字符集与长度
        由 build_headers -> _valid_token 把关。
        """
        payload = sync_auth.build_payload(bad, _meta())
        with pytest.raises(signin.AuthError):
            signin.build_headers(payload)

    def test_missing_uid_rejected(self):
        payload = sync_auth.build_payload(TOKEN, _meta(uid=""))
        with pytest.raises(signin.AuthError):
            signin._validate_session(payload)


class TestHelpers:
    def test_shell_quote_rejects_injection(self):
        assert sync_auth._shell_quote("/opt/x") == "'/opt/x'"
        for bad in ["", "a'b", "a\nb", "a\x00b"]:
            with pytest.raises(ValueError):
                sync_auth._shell_quote(bad)

    def test_days_left(self):
        # _days_left 向下取整，所以留出余量避免浮点边界把 5 算成 4；
        # 过期侧同理：-2.0014 天会被算成 -3（floor 除法），只要判负即可。
        now_ms = int(time.time()) * 1000
        assert sync_auth._days_left(now_ms + (5 * 86400 + 120) * 1000) == 5
        assert sync_auth._days_left(now_ms - (2 * 86400 + 120) * 1000) <= -2
        assert sync_auth._days_left(None) is None
        assert sync_auth._days_left("not-a-number") is None

    def test_ms_to_iso(self):
        assert sync_auth._ms_to_iso(1796262562984).startswith("2026-12-03")
        assert sync_auth._ms_to_iso(None) == ""
        assert sync_auth._ms_to_iso("x") == ""

    def test_ts_to_text(self):
        assert sync_auth._ts_to_text(0) != "未知"      # 1970 也算有效时间戳
        assert sync_auth._ts_to_text(None) == "未知"

    def test_token_fingerprint_stable(self):
        a = sync_auth._token_fingerprint(TOKEN)
        assert a == sync_auth._token_fingerprint(TOKEN)
        assert len(a) == 16
        assert TOKEN not in a


class TestConfig:
    def test_defaults_are_placeholders(self):
        """仓库里不能有真实内网地址。"""
        cfg = dict(sync_auth.DEFAULTS)
        assert cfg["host"] == "192.168.1.100"
        assert cfg["user"] == "root"

    def test_remote_auth_derived_from_dir(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        args = type("A", (), {"host": None, "user": None, "port": None,
                              "identity": None, "remote_dir": "/srv/wb",
                              "remote_python": None, "ssh": None})()
        cfg = sync_auth._merge_config(args)
        assert cfg["remote_auth"] == "/srv/wb/auth.json"
        assert cfg["remote_dir"] == "/srv/wb"      # 尾部斜杠被去掉

    def test_env_overrides(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SYNC_HOST", "10.0.0.9")
        monkeypatch.setenv("SYNC_PORT", "2222")
        args = type("A", (), {"host": None, "user": None, "port": None,
                              "identity": None, "remote_dir": None,
                              "remote_python": None, "ssh": None})()
        cfg = sync_auth._merge_config(args)
        assert cfg["host"] == "10.0.0.9"
        assert cfg["port"] == 2222


class TestSshDiscovery:
    def test_prefers_native_openssh_on_windows(self):
        if os.name != "nt":
            pytest.skip("仅 Windows")
        found = sync_auth.find_ssh()
        assert found is not None
        # 必须避开 MSYS/Git 版：它会把远端 /opt/... 改写成 Windows 路径
        assert "System32" in found or "OpenSSH" in found or "\\" in found
        assert "PortableGit" not in found
