<#
赛博财务管家启动脚本

    .\run.ps1              # 后台静默启动（无控制台窗口）
    .\run.ps1 -Console     # 前台启动，显示日志，Ctrl+C 退出
    .\run.ps1 -Status      # 查看是否在运行 / 是否已设置自启
    .\run.ps1 -Where       # 打印当前识别到的桌宠矩形
    .\run.ps1 -AutoDetect  # 自动探测桌宠真实范围（推荐，无需手动点击）
    .\run.ps1 -Calibrate   # 手动校准：点一次桌宠
    .\run.ps1 -Install     # 设置开机自启并立即启动
    .\run.ps1 -Uninstall   # 取消自启并停止
    .\run.ps1 -Preview out # 导出气泡预览图
    .\run.ps1 -Test        # 跑单元测试

Python 解析顺序：$env:FINANCE_PET_PYTHON -> Codex 自带运行时 -> py -> python
#>
param(
    [switch]$Console,
    [switch]$Where,
    [switch]$Calibrate,
    [switch]$AutoDetect,
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$Test,
    [switch]$Show,
    [string]$Preview
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Find-Python {
    if ($env:FINANCE_PET_PYTHON -and (Test-Path $env:FINANCE_PET_PYTHON)) {
        return $env:FINANCE_PET_PYTHON
    }
    $candidates = @(
        (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"),
        (Join-Path $env:LOCALAPPDATA "OpenAI\Codex\runtimes\codex-primary-runtime\dependencies\python\python.exe")
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    foreach ($cmd in @("py", "python")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    throw "找不到 Python。请安装 Python 3.11+ 或设置 `$env:FINANCE_PET_PYTHON。"
}

$python = Find-Python
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
$entry = Join-Path $root "run_pet.py"
$startup = [Environment]::GetFolderPath("Startup")
$shortcut = Join-Path $startup "赛博财务管家.lnk"

function Get-PetProcesses {
    Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*finance_pet*" -or $_.CommandLine -like "*run_pet.py*" }
}

function Show-Status {
    $procs = @(Get-PetProcesses)
    if ($procs.Count -gt 0) {
        Write-Host "运行中：$($procs.Count) 个进程 (PID $($procs.ProcessId -join ', '))" -ForegroundColor Green
    } else {
        Write-Host "未运行。执行 .\run.ps1 启动，或用 .\run.ps1 -Install 设置开机自启。" -ForegroundColor Yellow
    }
    $auto = if (Test-Path $shortcut) { "已设置 ($shortcut)" } else { "未设置" }
    Write-Host "开机自启：$auto"
    $cfg = Join-Path $env:USERPROFILE ".codex\finance-pet\config.json"
    if (Test-Path $cfg) {
        Write-Host "配置：$cfg"
        Get-Content $cfg -Raw
    }
}

if ($Status) { Show-Status; exit 0 }

if ($Uninstall) {
    if (Test-Path $shortcut) { Remove-Item $shortcut -Force }
    foreach ($p in @(Get-PetProcesses)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "已取消开机自启并停止挂件。" -ForegroundColor Green
    exit 0
}

Write-Host "使用 Python: $python" -ForegroundColor DarkGray
& $python -c "import PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少 Pillow（气泡渲染需要）。请执行：" -ForegroundColor Yellow
    Write-Host "  & `"$python`" -m pip install pillow" -ForegroundColor Yellow
    exit 1
}

$env:PYTHONPATH = $root

if ($Test) {
    & $python -m unittest discover -s (Join-Path $root "tests") -t $root -v
    exit $LASTEXITCODE
}

if ($Install) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($shortcut)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "`"$entry`""
    $lnk.WorkingDirectory = $root
    $lnk.WindowStyle = 7
    $lnk.Description = "赛博财务管家（Codex 桌宠 DeepSeek 余额面板）"
    $lnk.Save()
    Write-Host "已设置开机自启：$shortcut" -ForegroundColor Green
    if (@(Get-PetProcesses).Count -eq 0) {
        Start-Process -FilePath $pythonw -ArgumentList "`"$entry`"" -WorkingDirectory $root -WindowStyle Hidden
        Write-Host "并已启动挂件。点击 Codex 桌宠即可查看余额。" -ForegroundColor Green
    } else {
        Write-Host "挂件已在运行中。" -ForegroundColor Green
    }
    exit 0
}

$cliArgs = @("-m", "finance_pet")
if ($Where) { $cliArgs += "--where" }
if ($Calibrate) { $cliArgs += "--calibrate" }
if ($AutoDetect) { $cliArgs += "--autodetect" }
if ($Show) { $cliArgs += "--show" }
if ($Preview) { $cliArgs += @("--preview", $Preview) }

if ($Where -or $Calibrate -or $AutoDetect -or $Preview -or $Console -or $Show) {
    & $python @cliArgs
    exit $LASTEXITCODE
}

if (@(Get-PetProcesses).Count -gt 0) {
    Write-Host "赛博财务管家已经在运行了。" -ForegroundColor Green
    exit 0
}

if (Test-Path $pythonw) {
    Start-Process -FilePath $pythonw -ArgumentList "`"$entry`"" -WorkingDirectory $root -WindowStyle Hidden
} else {
    Start-Process -FilePath $python -ArgumentList $cliArgs -WorkingDirectory $root -WindowStyle Hidden
}
Write-Host "赛博财务管家已在后台启动。点击 Codex 桌宠即可查看余额。" -ForegroundColor Green
