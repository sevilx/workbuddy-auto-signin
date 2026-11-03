# 在局域网 Linux 上跑签到（Windows 提供凭据）

> 下文示例主机为 `192.168.1.100`，请替换成你自己的 Linux 主机地址。
>
> 适用场景：WorkBuddy 桌面端装在 Windows，但那台机器不常开；想让它一直开着的 Linux 小主机（NAS / 树莓派 / Debian 服务器）代跑每日签到。

## 为什么不能直接拷凭据文件

`workbuddy-desktop.info` 里 `accessToken` 是 `$wbEncrypted` 信封：

```json
{ "$wbEncrypted": 1, "envelope": "base64(...suite=1,keyId,nonce,authTag,ciphertext...)" }
```

解密密钥来自客户端 Native Storage（`electron_browser_workbuddy_storage`），**与 Windows 机器绑定**。Linux 上没有 WorkBuddy 运行时，拿到文件也解不开，`signin.py doctor` 会直接报 `AUTH_ERROR / RUNTIME_UNAVAILABLE`。

所以只有一条路：**在 Windows 上解密成明文，再同步过去**。

## 架构

```
┌─ Windows（凭据源）───────────────┐        ┌─ Debian 192.168.1.100（执行端）──┐
│ WorkBuddy 桌面端                  │        │ /opt/workbuddy-auto-signin/       │
│   └ workbuddy-desktop.info        │  SSH   │   ├ signin.py   （原版，不改）    │
│ sync-auth.py                      │ ─────► │   ├ auth.json   （600，明文）     │
│   ├ 复用 signin.py 解密 sym-v1    │  scp   │   └ run.sh                        │
│   └ 生成最小明文凭据并推送         │        │ systemd:                          │
│ 计划任务 WorkBuddyAuthSync / 2h   │        │   workbuddy-signin.timer  00:05   │
└──────────────────────────────────┘        │   workbuddy-signin-poll.timer 4h  │
                                            └───────────────────────────────────┘
```

远端 `signin.py` 一行都不用改——它支持 `WORKBUDDY_AUTH_FILE` 覆盖凭据路径，`run.sh` 里设好即可。

## 一次性部署

### 1. Windows：生成密钥并装到远端

```bash
ssh-keygen -t ed25519 -N "" -C "workbuddy-auto-signin@$(hostname)" -f ~/.ssh/workbuddy_debian
```

把公钥装到 Debian（会提示输入一次 root 密码）：

```powershell
type $env:USERPROFILE\.ssh\workbuddy_debian.pub | & "$env:SystemRoot\System32\OpenSSH\ssh.exe" root@192.168.1.100 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

### 2. Windows：确认配置

`sync-config.json`（从 `sync-config.example.json` 复制）：

```json
{
  "host": "192.168.1.100",
  "user": "root",
  "port": 22,
  "identity": "~/.ssh/workbuddy_debian",
  "remote_dir": "/opt/workbuddy-auto-signin",
  "remote_python": "python3",
  "expiry_warn_days": 3,
  "force_repush_days": 7
}
```

### 3. 部署

```bash
python sync-auth.py doctor    # 先确认 SSH 通
python sync-auth.py deploy    # 上传脚本 + 装 systemd + 推凭据 + 远端验证
```

成功时输出 `DEPLOYED`，远端状态检查 `HTTP 200`。

### 4. Windows：注册定时同步

```powershell
powershell -ExecutionPolicy Bypass -File .\install-sync-windows.ps1
```

任务 `WorkBuddyAuthSync`，每 2 小时一次。凭据没变化时脚本内部直接跳过（不起客户端子进程），所以高频轮询几乎没有开销；真刷新了 token 也能在 2 小时内同步过去。

## 日常维护

| 操作 | 命令 |
|---|---|
| 立即同步一次 | `python sync-auth.py push --force` |
| 看远端签到状态 | `python sync-auth.py check` |
| Windows 侧同步日志 | `sync.log`（只在真推送或出错时写） |
| 远端签到日志 | `ssh root@192.168.1.100 tail -n 20 /var/log/workbuddy-signin.log` |
| 远端定时器 | `ssh root@192.168.1.100 systemctl list-timers 'workbuddy-signin*'` |
| 卸载远端 | `ssh root@192.168.1.100 'systemctl disable --now workbuddy-signin.timer workbuddy-signin-poll.timer'` |
| 卸载 Windows 同步任务 | `powershell -ExecutionPolicy Bypass -File .\install-sync-windows.ps1 -Remove` |

## Token 生命周期

| 字段 | 时长 | 说明 |
|---|---|---|
| `expiresIn` | 30 天 | access token |
| `refreshExpiresIn` | 60 天 | refresh token |
| `lastRefreshTime` | — | 桌面端最近一次续期时间 |

续期由 **WorkBuddy 桌面端**负责，本方案不碰 refresh token（避免和客户端争抢令牌轮换）。

因此：

- Windows 每天/每几小时开机一次 → 桌面端自动续期 → 同步任务把新 token 推过去，长期无感
- Windows 连续 30 天不开机 → 远端会 `AUTH_REJECTED / HTTP 401`，同步脚本在剩余 3 天时就会在 `sync.log` 里记 `needs_attention: true`
- **兜底**：在 Debian 上 `apt install openssh-server` 之后，也可以反向让 Debian 拉取——但更简单的做法是把 `install-windows.ps1` 的两个本地签到任务留着，两边都签也无妨（接口幂等）。注意两边同时开时，成长中心的 Buddy 派出名额只算一次，重复的那次服务端会返回 `daily_limit_reached`，属正常。

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `Permission denied (publickey)` | 公钥没装到远端，重做第 1 步 |
| `Host key verification failed` | 远端重装过系统，删掉 `C:\Users\<你>\.ssh\known_hosts` 里对应行 |
| 远端 `NO_AUTH` | `auth.json` 还没推送，或 `run.sh` 里的路径不对 |
| 远端 `AUTH_ERROR / INVALID_FORMAT` | 推送的文件被破坏；`python sync-auth.py push --force` 重推 |
| 远端 `AUTH_REJECTED / HTTP 401` | token 过期，打开 Windows 上的 WorkBuddy 桌面端登录一次，再 `push --force` |
| `deploy` 卡在 `mkdir` | Windows 侧的 ssh 用了 Git/MSYS 版，会把 `/opt/...` 改写成 Windows 路径。`sync-auth.py` 已强制优先系统 OpenSSH，若仍异常检查 `doctor` 输出的 `ssh` 字段 |
| 注册任务时「拒绝访问」 | `-AtStartup` 需要管理员；`-AtLogOn` 在组策略收紧的机器上同样被拒（本机实测两者都失败）。脚本只用「一次性 + 重复间隔」触发器，配合 `StartWhenAvailable` 在开机后补跑 |
| 任务结果码 0 但远端没更新 | 凭据未变化，被缓存跳过（`sync.log` 无记录是正常的）。要强制验证用 `python sync-auth.py push --force` |
| 任务指向 `WindowsApps\pythonw.exe` | 那是 Store 应用别名 stub，后台跑不起来。`install-sync-windows.ps1` 已排除该路径，优先用 `.workbuddy\binaries\python\versions\*\pythonw.exe` |

## 安全边界

- 明文 `auth.json` 只存在于两台机器：Windows 的 `auth.export.json`（仅在你显式 `export` 时产生，0600 + icacls 收紧）和远端 `auth.json`（0600，root 所有）
- 传输走 SSH，token 只经 stdin，不进命令行参数（不会出现在进程列表里）
- 脚本永不打印 token；`sync.log` 只记录结果与过期时间
- 远端只监听出站，不开放任何端口
