# 维护这个 fork

本仓库是 [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin) 的 fork，
在上游基础上加了「Windows → 局域网 Linux」的跨机方案。

## 三个 remote

| remote | 地址 | 用途 |
|---|---|---|
| `origin` | `github.com/sevilx/workbuddy-auto-signin` | 自己的 fork，日常推这里 |
| `upstream` | `github.com/88lin/workbuddy-auto-signin` | 原作者，只读 |

## 本 fork 独有的文件

上游没有、合并时**整段保留**的部分：

| 文件 | 作用 |
|---|---|
| `sync-auth.py` | Windows 侧解密凭据并推送到 Linux |
| `install-debian.sh` | Linux 侧 systemd timer 安装 |
| `install-sync-windows.ps1` | Windows 侧定时同步任务 |
| `update-upstream.ps1` | 本文件所描述的上游合并脚本 |
| `sync-config.example.json` | 同步配置模板 |
| `docs/remote-debian.md` | 跨机部署说明 |
| `docs/fork-maintenance.md` | 本文件 |
| `tests/test_sync_auth.py` | 契约测试：生成的明文凭据必须仍被 `signin.py` 接受 |

最后那个测试是刻意加的：上游一旦调整 `_validate_session` / `build_headers` 的字段要求，
CI 会在合并时立刻发现本 fork 的凭据格式不再兼容，而不是等到第二天签到失败才暴露。

额外修改过的上游文件（合并时**容易冲突**，只有两处）：

- `README.md` —— 在「模式 B」末尾插了「🐧 Linux / NAS · Windows 提供凭据（跨机）」小节
- `.gitignore` —— 追加了 `auth.export.json` / `.sync-cache.json` / `sync.log` / `sync-config.json`

## 上游更新了怎么办

先预览（不改动任何东西）：

```powershell
powershell -ExecutionPolicy Bypass -File .\update-upstream.ps1
```

它会 `git fetch upstream`、列出落后多少提交、改了哪些文件。

确认后合并并推送：

```powershell
powershell -ExecutionPolicy Bypass -File .\update-upstream.ps1 -Apply
```

加 `-NoPush` 可以只合并不推送，先本地验证。

### 撞冲突时

冲突概率很低：上游的改动集中在 `signin.py`，本 fork 没碰它。真撞上基本只会在 `README.md`。

处理原则：**上游正文照收，本 fork 的小节贴回去**。

```bash
git status                       # 看哪些文件 UU
# 手工编辑，保留双方内容
git add -A && git commit --no-edit
git push origin main
```

放弃这次合并：`git merge --abort`

## 合并后别忘了远端

`signin.py` 若有实质变动，Debian 上跑的还是旧版。重新部署一次即可（会覆盖 `signin.py` 和 systemd unit，**不动 `auth.json`**）：

```bash
python sync-auth.py deploy
```

`deploy` 结尾会跑一次远端只读状态查询；`HTTP 200` 就说明新版本正常。
