@echo off
setlocal

set "ENV_NAME=%APP_CONDA_ENV%"
if "%ENV_NAME%"=="" set "ENV_NAME=any-auto-register"
set "HOST=%HOST%"
if "%HOST%"=="" set "HOST=0.0.0.0"
set "PORT=%PORT%"
if "%PORT%"=="" set "PORT=8000"
set "RESTART_EXISTING=%RESTART_EXISTING%"
if "%RESTART_EXISTING%"=="" set "RESTART_EXISTING=1"

where conda >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未找到 conda 命令。请先安装 Miniconda/Anaconda，并确保 conda 可在终端中使用。
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 未找到 npm 命令。请先安装 Node.js 18+，并确保 npm 可在终端中使用。
  exit /b 1
)

cd /d "%~dp0"
echo [INFO] 项目目录: %CD%
echo [INFO] 使用 conda 环境: %ENV_NAME%
echo [INFO] 启动后端: http://localhost:%PORT%
echo [INFO] 按 Ctrl+C 可停止服务

if not exist "frontend\node_modules" (
  echo [INFO] 前端依赖未安装，正在安装
  pushd "frontend"
  if exist "package-lock.json" (
    call npm ci
  ) else (
    call npm install
  )
  if errorlevel 1 (
    popd
    echo [ERROR] 前端依赖安装失败。
    exit /b 1
  )
  popd
)

echo [INFO] 正在构建前端
pushd "frontend"
call npm run build
if errorlevel 1 (
  popd
  echo [ERROR] 前端构建失败。
  exit /b 1
)
popd

if "%RESTART_EXISTING%"=="1" (
  echo [INFO] 启动前先清理旧的后端 / Solver 进程
  powershell -ExecutionPolicy Bypass -File "%~dp0stop_backend.ps1" -BackendPort %PORT% -SolverPort 8889 -FullStop 0
)

for /f "usebackq delims=" %%i in (`conda run --no-capture-output -n %ENV_NAME% python -c "import sys; print(sys.executable)"`) do set "PYTHON_EXE=%%i"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] 无法解析 conda 环境 "%ENV_NAME%" 对应的 python 路径。
  exit /b 1
)

set "HOST=%HOST%"
set "PORT=%PORT%"
echo [INFO] Python: %PYTHON_EXE%
"%PYTHON_EXE%" main.py