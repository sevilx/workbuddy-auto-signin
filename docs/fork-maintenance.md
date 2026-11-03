# 维护这个 fork

本仓库是 [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin) 的 fork，
在上游基础上加了「Windows 提供凭据 → 局域网 Linux」的跨机方案（部署见
[nas-deploy.md](nas-deploy.md)，日常只需三条命令：`setup` → `deploy` → 注册计划任务）。

## 两个 remote

| remote | 地址 | 用途 |
|---|---|---|
| `origin` | `github.com/sevilx/workbuddy-auto-signin` | 自己的 fork，日常推这里 |
| `upstream` | `github.com/88lin/workbuddy-auto-signin` | 原作者，只读 |

## 本 fork 独有的文件

上游没有、合并时**整段保留**的部分：

| 文件 | 作用 |
|---|---|
| `sync-auth.py` | Windows 侧解密凭据并推送到 Linux（含 `setup` 一键准备） |
| `install-debian.sh` | Linux 侧 systemd timer 安装 |
| `install-sync-windows.ps1` | Windows 侧定时同步任务 |
| `update-upstream.ps1` | 本文件所描述的上游合并脚本 |
| `sync-config.example.json` | 同步配置模板 |
| `docs/nas-deploy.md` | 跨机部署手册（案例式，含换机迁移） |
| `docs/fork-maintenance.md` | 本文件 |
| `tests/test_sync_auth.py` | 契约测试：生成的明文凭据必须仍被 `signin.py` 接受 |

最后那个测试是刻意加的：上游一旦调整 `_validate_session` / `build_headers` 的字段要求，
CI 会在合并时立刻发现本 fork 的凭据格式不再兼容，而不是等到第二天签到失败才暴露。

## 对上游文件只动了两处

为了把冲突面压到最小，对上游文件的改动被限制成两个位置：

| 文件 | 改动 | 形态 |
|---|---|---|
| `README.md` | 顶部一条 fork 说明 + 「模式 B」末尾的跨机小节 | 各用一个 `<!-- FORK:*-START -->` / `<!-- FORK:*-END -->` 包住 |
| `.gitignore` | 追加明文凭据、同步缓存、日志、本机配置 | 末尾几行，与上游条目不相邻 |

标记块的作用是让冲突处理变成机械动作：**上游正文照收，两个标记块及其中内容原样保留**。
`update-upstream.ps1` 会在合并成功后自动检查这些标记与上面 8 个文件是否都还在，
少一个就拒绝推送。

## CI

上游的 `tests.yml` 用 `python -m unittest discover -s tests`，跑 ubuntu / windows / macos ×
Python 3.10 / 3.13 六个矩阵。**不要引入 pytest**——CI 环境没装，会让六个 job 全报
`ModuleNotFoundError`（这个坑已经踩过一次）。

本地用同一条命令自检：

```bash
python -m unittest discover -s tests
```

fork 的 push 不一定自动触发 CI。手动跑：

```bash
gh workflow run tests.yml --repo sevilx/workbuddy-auto-signin --ref main
gh run list --repo sevilx/workbuddy-auto-signin --limit 1
```

注意 `--repo` 不能省：在本仓库目录里执行 `gh workflow run` 时，gh 可能按 workflow ID
解析到**上游**仓库，直接 403（没有上游 admin 权限）。

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

合并成功后脚本会自己跑两道检查，任一不过就**不会推送**（想单独复查就加 `-VerifyOnly`，
它不联网、不动工作区，手工解决冲突后特别有用）：

1. `Test-ForkIntact` —— 8 个独有文件在不在、README 的两个 FORK 标记块在不在
2. `python -m py_compile signin.py` —— 上游合并结果语法是否正常

### 撞冲突时

冲突概率很低：上游的改动集中在 `signin.py`，本 fork 没碰它。真撞上基本只会在 `README.md`。

处理原则：**上游正文照收，两个 `FORK` 标记块原样贴回**。

```bash
git status                       # 看哪些文件 UU
# 手工编辑，保留双方内容
git add -A && git commit --no-edit
powershell -ExecutionPolicy Bypass -File .\update-upstream.ps1 -VerifyOnly   # 只复查标记，不联网
git push origin main
```

放弃这次合并：`git merge --abort`

## 合并后别忘了远端

`signin.py` 若有实质变动，Debian 上跑的还是旧版。重新部署一次即可（会覆盖 `signin.py` 和 systemd unit，**不动 `auth.json`**）：

```bash
python sync-auth.py deploy
```

`deploy` 结尾会跑一次远端只读状态查询；`HTTP 200` 就说明新版本正常。
