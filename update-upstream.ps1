# update-upstream.ps1 — 把上游 88lin/workbuddy-auto-signin 的更新合并进本 fork
#
# 作者：Ben
#
# 用法（在仓库目录下）：
#     powershell -ExecutionPolicy Bypass -File .\update-upstream.ps1           # 只看有什么更新
#     powershell -ExecutionPolicy Bypass -File .\update-upstream.ps1 -Apply    # 真的合并并推送
#
# 工作方式：
#   origin   = https://github.com/sevilx/workbuddy-auto-signin     （你的 fork）
#   upstream = https://github.com/88lin/workbuddy-auto-signin      （原作者）
#   本地改动只在 5 个新增文件 + README/.gitignore 上，上游改动集中在 signin.py，
#   冲突面很小；万一 README 撞车，按提示保留双方内容即可。

param(
    [switch]$Apply,
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

function Fail($msg) {
    Write-Host ""
    Write-Host "已中止：$msg" -ForegroundColor Red
    exit 1
}

# --- 0. 前置检查 ---
if (-not (Test-Path ".git")) { Fail "当前目录不是 git 仓库（请先 cd 到 workbuddy-auto-signin）" }

$remotes = (git remote) -split "`n" | ForEach-Object { $_.Trim() }
if ($remotes -notcontains "upstream") {
    Write-Host "[!] 未配置 upstream，正在添加 ..." -ForegroundColor Yellow
    git remote add upstream https://github.com/88lin/workbuddy-auto-signin.git
}

$dirty = (git status --porcelain) -join "`n"
if ($dirty.Trim()) {
    Write-Host "工作区有未提交的改动：" -ForegroundColor Yellow
    git status --short
    Fail "先提交或 stash 再更新（避免合并时把本地改动搅在一起）"
}

# --- 1. 拉取上游 ---
Write-Host "[1/4] 拉取 upstream ..." -NoNewline
git fetch upstream --tags --quiet
if ($LASTEXITCODE -ne 0) { Write-Host ""; Fail "git fetch upstream 失败（网络或代理问题）" }
Write-Host " 完成" -ForegroundColor Green

$behind = [int](git rev-list --count "HEAD..upstream/main")
$ahead  = [int](git rev-list --count "upstream/main..HEAD")

Write-Host ""
Write-Host "相对上游：落后 $behind 个提交，领先 $ahead 个提交" -ForegroundColor Cyan

if ($behind -eq 0) {
    Write-Host "已是最新，无需更新。" -ForegroundColor Green
    exit 0
}

Write-Host "`n[2/4] 上游新增的提交：" -ForegroundColor Cyan
git log --oneline --no-decorate "HEAD..upstream/main"

Write-Host "`n[3/4] 改动文件：" -ForegroundColor Cyan
git diff --stat "HEAD...upstream/main"

if (-not $Apply) {
    Write-Host "`n以上为预览。确认无误后加 -Apply 真正合并：" -ForegroundColor Yellow
    Write-Host "    powershell -ExecutionPolicy Bypass -File .\update-upstream.ps1 -Apply"
    exit 0
}

# --- 2. 合并 ---
Write-Host "`n[4/4] 合并 upstream/main ..." -ForegroundColor Cyan
git merge --no-edit upstream/main
if ($LASTEXITCODE -ne 0) {
    $conflicted = (git diff --name-only --diff-filter=U) -join "`n"
    Write-Host ""
    Write-Host "有冲突，需要手工处理：" -ForegroundColor Red
    Write-Host $conflicted
    Write-Host @"

处理建议：
  1. 打开上面每个文件，搜索 <<<<<<< / ======= / >>>>>>>
  2. 本 fork 独有的部分（sync-auth.py / install-debian.sh / install-sync-windows.ps1 /
     docs/remote-debian.md）通常整段保留
  3. README.md 若冲突：上游的正文照收，再把本 fork 的「🐧 Linux / NAS」小节贴回去
  4. 改完执行：
        git add -A
        git commit --no-edit
        git push origin main
  5. 中途想放弃：git merge --abort

"@ -ForegroundColor Yellow
    exit 1
}

Write-Host "合并成功" -ForegroundColor Green

# --- 3. 冒烟验证 ---
Write-Host "`n[验证] signin.py 语法 ..." -NoNewline
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($py) {
    & $py -m py_compile signin.py
    if ($LASTEXITCODE -eq 0) { Write-Host " OK" -ForegroundColor Green }
    else { Write-Host " 失败，请检查合并结果" -ForegroundColor Red; exit 1 }
} else {
    Write-Host " 跳过（未找到 python）" -ForegroundColor Yellow
}

# --- 4. 推送 ---
if ($NoPush) {
    Write-Host "`n已合并但未推送（-NoPush）。确认后执行：git push origin main" -ForegroundColor Yellow
    exit 0
}

Write-Host "`n[推送] origin main ..." -NoNewline
git push origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Fail "推送失败。合并结果已保存在本地，可稍后重试 git push origin main"
}
Write-Host " 完成" -ForegroundColor Green

Write-Host "`n上游更新已合并并推送。" -ForegroundColor Green
Write-Host "提示：若 signin.py 有较大变动，远端也要同步一份：" -ForegroundColor Cyan
Write-Host "    python sync-auth.py deploy"
