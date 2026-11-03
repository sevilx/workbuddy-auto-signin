# install-sync-windows.ps1 — 注册「凭据同步到 Debian」的 Windows 计划任务
#
# 作者：Ben（基于 88lin/workbuddy-auto-signin）
#
# 用法（在仓库目录下，普通 PowerShell 即可，不需要管理员）：
#     powershell -ExecutionPolicy Bypass -File .\install-sync-windows.ps1
# 卸载：
#     powershell -ExecutionPolicy Bypass -File .\install-sync-windows.ps1 -Remove
#
# 任务名：WorkBuddyAuthSync，每 2 小时把本机解密出的明文凭据推到远端。
# 凭据未变化时脚本内部会跳过（不会每次起客户端子进程），空跑不写日志。

param(
    [switch]$Remove,
    [string]$ManualPythonw = "",   # 自动探测失败时手动填 pythonw.exe 完整路径
    [int]$IntervalHours = 2
)

$ErrorActionPreference = "Stop"
$TaskName = "WorkBuddyAuthSync"

function Test-RealPythonw([string]$p) {
    # WindowsApps 下的是 Microsoft Store 的应用执行别名（stub），
    # 计划任务里调用它不会真正启动解释器，必须排除。
    if (-not $p) { return $false }
    if ($p -like "*\WindowsApps\*") { return $false }
    return (Test-Path -LiteralPath $p)
}

function Find-Pythonw {
    $c = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (Test-RealPythonw $c) { return $c }
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) {
        $c = Join-Path (Split-Path $py) "pythonw.exe"
        if (Test-RealPythonw $c) { return $c }
    }
    foreach ($pat in @(
        "$env:USERPROFILE\.workbuddy\binaries\python\versions\*\pythonw.exe",
        "C:\Python3*\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python3*\pythonw.exe",
        "C:\Program Files\Python3*\pythonw.exe"
    )) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue |
               Where-Object { Test-RealPythonw $_.FullName } |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已删除计划任务 $TaskName" -ForegroundColor Green
    } else {
        Write-Host "计划任务 $TaskName 不存在，无需删除" -ForegroundColor Yellow
    }
    return
}

# --- 1. 定位解释器与脚本 ---
Write-Host "[1/3] 定位 pythonw.exe ..." -NoNewline
$manualOk = $ManualPythonw -and (Test-RealPythonw $ManualPythonw)
$pythonw = if ($manualOk) { $ManualPythonw } else { Find-Pythonw }
if (-not $pythonw) {
    Write-Host ""
    Write-Host "没找到 pythonw.exe。请在本脚本顶部用 -ManualPythonw 指定完整路径。" -ForegroundColor Yellow
    exit 1
}
Write-Host " $pythonw" -ForegroundColor Green

$sync = Join-Path $PSScriptRoot "sync-auth.py"
if (-not (Test-Path -LiteralPath $sync)) {
    Write-Host "找不到 sync-auth.py（应位于 $PSScriptRoot）" -ForegroundColor Red
    exit 1
}
$config = Join-Path $PSScriptRoot "sync-config.json"
if (-not (Test-Path -LiteralPath $config)) {
    Write-Host "[!] 缺少 sync-config.json，脚本将使用内置默认值（192.168.1.100 / root）" -ForegroundColor Yellow
}

# --- 2. 注册任务 ---
Write-Host "[2/3] 注册计划任务 $TaskName ..." -NoNewline
$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$sync`" silent-push" -WorkingDirectory $PSScriptRoot -ErrorAction Stop

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) -ErrorAction Stop

# 只用「一次性 + 重复间隔」这一种触发器。
# -AtStartup 需要管理员权限，-AtLogOn 在组策略收紧的机器上同样会被拒绝访问，
# 两者都试过，普通账号注册不来。重复触发器 + StartWhenAvailable 已经够用：
# 关机错过的轮次会在下次开机后补跑。
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -ErrorAction Stop

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "每 $IntervalHours 小时把 WorkBuddy 明文凭据同步到局域网 Debian" `
        -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Host ""
    Write-Host "注册失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "如提示拒绝访问，请用与登录 WorkBuddy 相同的账号重试。" -ForegroundColor Yellow
    exit 1
}
Write-Host " 完成" -ForegroundColor Green

# --- 3. 立即验证一次 ---
Write-Host "[3/3] 立即执行一次以验证 ..." -NoNewline
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host (" 上次结果码 {0}，下次运行 {1}" -f $info.LastTaskResult, $info.NextRunTime) -ForegroundColor Green

$log = Join-Path $PSScriptRoot "sync.log"
if (Test-Path -LiteralPath $log) {
    Write-Host "`n最近一条同步记录：" -ForegroundColor Cyan
    Get-Content -LiteralPath $log -Tail 1
} else {
    Write-Host "`n尚无同步记录（可能是首次运行尚未完成，或凭据未变化被跳过）。" -ForegroundColor Yellow
    Write-Host "可手动排查：python `"$sync`" doctor" -ForegroundColor Yellow
}

Write-Host "`n完成。定时任务 $TaskName 已就位，每 $IntervalHours 小时同步一次。" -ForegroundColor Green
