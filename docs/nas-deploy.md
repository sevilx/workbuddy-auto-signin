# 案例：Windows 提供凭据，局域网 NAS 定时签到

> **适用场景**：WorkBuddy 桌面端只装在 Windows 上（凭据只能由它产生），但那台机器不常开；
> 家里一直开着的 Linux 小主机（NAS / 树莓派 / Debian 服务器）代跑每日签到。
>
> **一句话原理**：Windows 负责「解密凭据 + 推送」，NAS 负责「按点执行」。两边走 SSH，
> NAS 上的 `signin.py` 是**原版未改动**的。

> [!NOTE]
> 本文所有地址与账号都是占位示例，请替换成你自己的：

| 占位符 | 示例值 | 含义 |
|---|---|---|
| `<NAS_IP>` | `192.168.1.100` | NAS 的局域网地址 |
| `<NAS_USER>` | `root` | NAS 上执行安装的账号（需 root 或免密 sudo） |
| `<SSH_KEY>` | `~/.ssh/workbuddy_debian` | Windows 侧的专用私钥，别复用日常登录密钥 |

## 为什么不能直接把凭据文件拷过去

`workbuddy-desktop.info` 里 `accessToken` 是 `$wbEncrypted` 信封：

```json
{ "$wbEncrypted": 1, "envelope": "base64(...suite=1,keyId,nonce,authTag,ciphertext...)" }
```

解密密钥来自客户端原生存储（`electron_browser_workbuddy_storage`），**与那台 Windows 机器绑定**。
Linux 上没有 WorkBuddy 运行时，拿到文件也解不开，`signin.py doctor` 会直接报
`AUTH_ERROR / RUNTIME_UNAVAILABLE`。

所以只有一条路：**在 Windows 上解密成明文，再同步过去**。

## 架构

```
┌─ Windows（凭据源，可以不管时间）──┐        ┌─ NAS <NAS_IP>（执行端，常开）─────┐
│ WorkBuddy 桌面端                  │        │ /opt/workbuddy-auto-signin/       │
│   └ workbuddy-desktop.info        │  SSH   │   ├ signin.py   （原版，不改）    │
│ sync-auth.py                      │ ─────► │   ├ auth.json   （600，明文）     │
│   ├ 复用 signin.py 解密 sym-v1    │  stdin │   └ run.sh                        │
│   └ 生成最小明文凭据并推送        │        │ systemd:                          │
│ 计划任务 WorkBuddyAuthSync / 2h   │        │   workbuddy-signin.timer   00:05  │
└──────────────────────────────────┘        │   workbuddy-signin-poll.timer 4h  │
                                            └───────────────────────────────────┘
```

远端 `signin.py` 一行都不用改——它支持 `WORKBUDDY_AUTH_FILE` 覆盖凭据路径，`run.sh` 里设好即可。

## 前置条件

**Windows（凭据源）**

- 已安装并**登录过** WorkBuddy 桌面端
- Python 3（任意版本，无第三方依赖）；`git` 可选
- 有 `ssh` 客户端：`Windows 设置 → 应用 → 可选功能 → 添加 OpenSSH 客户端`

**NAS（执行端）**

- Debian 11+ / Ubuntu 20.04+（带时区的 `OnCalendar` 需要 systemd 249+）
- `python3`（3.8+）、`openssh-server`（`apt install openssh-server`）
- 安装需要 **root 或免密 sudo**：系统级 systemd unit 与 `/var/log` 都要权限
- 能出网访问 `copilot.tencent.com`

## 部署：四步

### 1. Windows：克隆并准备

```bash
git clone https://github.com/sevilx/workbuddy-auto-signin.git
cd workbuddy-auto-signin
python sync-auth.py setup --host <NAS_IP>
```

`setup` 一次性做完三件事，且都是**幂等**的：

| 动作 | 说明 |
|---|---|
| 检查本机凭据 | 找不到就直说「先装桌面端并登录」，不猜路径 |
| 生成 SSH 密钥 | 默认 `~/.ssh/workbuddy_debian`（ed25519，无口令），已存在则复用 |
| 写 `sync-config.json` | 记住 host / user / 端口，后续命令不用再带参数 |

跑完输出两种结果之一：

- `SETUP_READY` —— SSH 已通，直接做第 3 步
- `NEED_KEY_AUTH` —— 还差授权，把输出里的 `authorize_commands.powershell` 拿去执行

### 2. Windows：把公钥装到 NAS（只需一次）

复制 `setup` 输出的 `authorize_commands.powershell` 粘到 **PowerShell** 执行，
它会提示输入一次 NAS 密码：

```powershell
type "$env:USERPROFILE\.ssh\workbuddy_debian.pub" | & "$env:SystemRoot\System32\OpenSSH\ssh.exe" <NAS_USER>@<NAS_IP> "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

> [!IMPORTANT]
> **为什么这一步必须你亲手做**：密码不该出现在脚本、配置文件或任何对话记录里。
> 私钥永远只留在 Windows，NAS 只拿得到公钥。

验证（应直接返回 `CONNECTED`，不再问密码）：

```powershell
& "$env:SystemRoot\System32\OpenSSH\ssh.exe" <NAS_USER>@<NAS_IP> "echo CONNECTED"
```

### 3. Windows：一键部署到 NAS

```bash
python sync-auth.py setup      # 再跑一次，确认变成 SETUP_READY
python sync-auth.py deploy
```

`deploy` 按顺序做：建目录 → 上传 `signin.py` 与 `install-debian.sh` →
远端执行安装（注册 systemd）→ 解密凭据并推送 → 远端只读状态查询。

成功时末尾是：

```json
{"result": "DEPLOYED", "report": "部署完成：… ；远端状态检查 HTTP 200", "needs_attention": false}
```

### 4. Windows：注册定时同步

```powershell
powershell -ExecutionPolicy Bypass -File .\install-sync-windows.ps1
```

任务 `WorkBuddyAuthSync` 每 2 小时跑一次。凭据密文没变化时脚本内部直接跳过
（不起 Electron 子进程），所以高频轮询几乎零开销；真续期了新 token 也能在 2 小时内同步过去。

## 验收清单

四条都过就算跑通：

| 检查 | 命令 | 期望 |
|---|---|---|
| 本机凭据可解密、SSH 通 | `python sync-auth.py doctor` | `SSH 连通，远端凭据已存在` |
| 远端定时器已注册 | `ssh <NAS_USER>@<NAS_IP> "systemctl list-timers 'workbuddy-signin*'"` | 两行，`NEXT` 是今天或明天凌晨 |
| 签到真的能跑 | `ssh <NAS_USER>@<NAS_IP> "systemctl start workbuddy-signin; tail -3 /var/log/workbuddy-signin.log"` | `成功领取 N 积分` 或 `今日已签过` |
| Windows 任务已生效 | `Get-ScheduledTaskInfo WorkBuddyAuthSync` | `LastTaskResult: 0`，`NextRunTime` 在 2 小时内 |

## 从另一台电脑重新部署

换机只需三步，**不用改任何代码**：

```bash
git clone https://github.com/sevilx/workbuddy-auto-signin.git
cd workbuddy-auto-signin
python sync-auth.py setup --host <NAS_IP>     # 生成新密钥，旧电脑的钥匙不受影响
```

然后按第 2 步授权、第 3 步 deploy、第 4 步注册计划任务。

> [!TIP]
> 两台电脑同时往同一台 NAS 推没问题：`auth.json` 是覆盖写，谁最后推谁生效，
> 两边 token 指向同一个账号。旧电脑退役时，把它的公钥从 NAS 的
> `~/.ssh/authorized_keys` 里删掉即可。

只想临时验证、不在新机器上留计划任务：

```bash
python sync-auth.py export      # 只导出明文到本地 auth.export.json（用完记得删）
python sync-auth.py check       # 只读远端状态，不会重复领取
```

## 日常维护

| 操作 | 命令 |
|---|---|
| 立即同步一次 | `python sync-auth.py push --force` |
| 看远端签到状态 | `python sync-auth.py check` |
| Windows 侧同步日志 | `sync.log`（只在真推送或出错时写） |
| 远端签到日志 | `ssh <NAS_USER>@<NAS_IP> tail -n 20 /var/log/workbuddy-signin.log` |
| 远端定时器 | `ssh <NAS_USER>@<NAS_IP> systemctl list-timers 'workbuddy-signin*'` |
| 上游更新后同步脚本 | `python sync-auth.py deploy`（覆盖 `signin.py` 与 unit，**不动 `auth.json`**） |
| 卸载远端 | `ssh <NAS_USER>@<NAS_IP> 'systemctl disable --now workbuddy-signin.timer workbuddy-signin-poll.timer'` |
| 卸载 Windows 同步任务 | `powershell -ExecutionPolicy Bypass -File .\install-sync-windows.ps1 -Remove` |

## Token 生命周期

| 字段 | 时长 | 说明 |
|---|---|---|
| `expiresIn` | 30 天 | access token |
| `refreshExpiresIn` | 60 天 | refresh token |
| `lastRefreshTime` | — | 桌面端最近一次续期时间 |

续期由 **WorkBuddy 桌面端**负责，本方案不碰 refresh token（避免和客户端争抢令牌轮换）。因此：

- Windows 每天或每几小时开机一次 → 桌面端自动续期 → 同步任务把新 token 推过去，长期无感
- Windows 连续 30 天不开机 → 远端 `AUTH_REJECTED / HTTP 401`；同步脚本在剩余 3 天时就会在
  `sync.log` 里记 `needs_attention: true`
- **兜底**：两边都签也无妨（接口幂等）。同时开时成长中心的 Buddy 派出名额只算一次，
  重复的那次服务端返回 `daily_limit_reached`，属正常

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `setup` 报「找不到 WorkBuddy 凭据」 | 当前 Windows 没装或没登录桌面端；或设 `WORKBUDDY_AUTH_FILE` 指向凭据文件 |
| `setup` 报「未找到 ssh 客户端」 | 装「OpenSSH 客户端」可选功能，或用 `--ssh` 指定 `ssh.exe` 路径 |
| `Permission denied (publickey)` | 公钥没装到 NAS，重做第 2 步 |
| `Host key verification failed` | NAS 重装过系统，删掉 `C:\Users\<你>\.ssh\known_hosts` 里对应行 |
| 远端 `NO_AUTH` | `auth.json` 还没推送，或 `run.sh` 里的路径不对 |
| 远端 `AUTH_ERROR / INVALID_FORMAT` | 推送的文件被破坏；`python sync-auth.py push --force` 重推 |
| 远端 `AUTH_REJECTED / HTTP 401` | token 过期；打开 Windows 上的桌面端登录一次，再 `push --force` |
| `deploy` 报 `sudo: a terminal is required` | 用了非 root 账号且 sudo 需要密码。给该账号配免密 sudo，或直接用 root |
| `deploy` 卡在 `mkdir` | Windows 侧的 ssh 是 Git/MSYS 版，会把 `/opt/...` 改写成 Windows 路径。`sync-auth.py` 已强制优先系统 OpenSSH；仍异常就看 `doctor` 输出的 `ssh` 字段 |
| 注册任务时「拒绝访问」 | `-AtStartup` 需要管理员；`-AtLogOn` 在组策略收紧的机器上同样被拒。脚本只用「一次性 + 重复间隔」触发器，配合 `StartWhenAvailable` 开机后补跑 |
| 任务结果码 0 但远端没更新 | 凭据未变化被缓存跳过（`sync.log` 无记录是正常的）。要强制验证用 `push --force` |
| 任务指向 `WindowsApps\pythonw.exe` | 那是 Store 应用别名 stub，后台跑不起来。安装脚本已排除该路径 |

## 安全边界

- 明文 `auth.json` 只存在于两台机器：Windows 的 `auth.export.json`（仅显式 `export` 时产生，
  0600 + icacls 收紧）和远端 `auth.json`（0600，属主是安装账号）
- 传输走 SSH，token 只经 stdin，不进命令行参数（不会出现在进程列表里）
- 脚本永不打印 token；`sync.log` 只记录结果与过期时间
- 远端只发出站请求，不新开监听端口
- 仓库里没有任何真实地址与账号，`sync-config.json` 被 `.gitignore` 排除
