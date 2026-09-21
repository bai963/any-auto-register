param(
    [string]$EnvName = "any-auto-register",
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8000,
    [switch]$RestartExisting = $true
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$conda = Get-Command conda -ErrorAction SilentlyContinue
if (-not $conda) {
    Write-Error "未找到 conda 命令。请先安装 Miniconda/Anaconda，并确保 conda 可在终端中使用。"
    exit 1
}

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    Write-Error "未找到 npm 命令。请先安装 Node.js 18+，并确保 npm 可在终端中使用。"
    exit 1
}

Write-Host "[INFO] 项目目录: $root"
Write-Host "[INFO] 使用 conda 环境: $EnvName"
$displayHost = if ($BindHost -eq "0.0.0.0") { "localhost" } else { $BindHost }
Write-Host "[INFO] 启动后端: http://$displayHost`:$Port"
Write-Host "[INFO] 按 Ctrl+C 可停止服务"

$frontendDir = Join-Path $root "frontend"
if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
    Write-Host "[INFO] 前端依赖未安装，正在安装"
    Push-Location $frontendDir
    try {
        if (Test-Path (Join-Path $frontendDir "package-lock.json")) {
            & npm ci
        } else {
            & npm install
        }
        if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败，退出码: $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

Write-Host "[INFO] 正在构建前端"
Push-Location $frontendDir
try {
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败，退出码: $LASTEXITCODE" }
} finally {
    Pop-Location
}

if ($RestartExisting) {
    Write-Host "[INFO] 启动前先清理旧的后端 / Solver 进程"
    & "$root\stop_backend.ps1" -BackendPort $Port -SolverPort 8889 -FullStop 0
}

$pythonExe = (conda run --no-capture-output -n $EnvName python -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path $pythonExe)) {
    Write-Error "无法解析 conda 环境 '$EnvName' 对应的 python 路径。"
    exit 1
}

$env:HOST = $BindHost
$env:PORT = [string]$Port

Write-Host "[INFO] Python: $pythonExe"
& $pythonExe main.py
