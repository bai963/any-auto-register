#!/bin/sh
# Linux backend startup script. Compatible with sh, bash and zsh.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ENV_NAME=${APP_CONDA_ENV:-any-auto-register}
BIND_HOST=${HOST:-0.0.0.0}
BACKEND_PORT=${PORT:-8000}
SOLVER_PORT=${SOLVER_PORT:-8889}
RESTART_EXISTING=${RESTART_EXISTING:-1}

if ! command -v conda >/dev/null 2>&1; then
    echo "[ERROR] conda command not found. Install Miniconda/Anaconda and initialize conda for this shell." >&2
    exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
    echo "[ERROR] npm command not found. Install Node.js 18+ and ensure npm is available in this shell." >&2
    exit 1
fi

case $BACKEND_PORT in
    ''|*[!0-9]*) echo "[ERROR] PORT must be a valid port number: $BACKEND_PORT" >&2; exit 1 ;;
esac

cd "$ROOT"

display_host=$BIND_HOST
if [ "$display_host" = "0.0.0.0" ]; then
    display_host=localhost
fi

echo "[INFO] Project directory: $ROOT"
echo "[INFO] Conda environment: $ENV_NAME"
echo "[INFO] Starting backend: http://$display_host:$BACKEND_PORT"
echo "[INFO] Press Ctrl+C to stop the service"

if [ ! -d "$ROOT/frontend/node_modules" ]; then
    echo "[INFO] Frontend dependencies are not installed; installing them"
    (
        cd "$ROOT/frontend"
        if [ -f package-lock.json ]; then
            npm ci
        else
            npm install
        fi
    )
fi

echo "[INFO] Building frontend"
(
    cd "$ROOT/frontend"
    npm run build
)

if [ "$RESTART_EXISTING" = "1" ]; then
    echo "[INFO] Cleaning up an existing backend / Solver process"
    BACKEND_PORT=$BACKEND_PORT SOLVER_PORT=$SOLVER_PORT FULL_STOP=0 "$ROOT/stop_backend.sh"
fi

PYTHON_EXE=$(conda run --no-capture-output -n "$ENV_NAME" python -c 'import sys; print(sys.executable)')
if [ ! -x "$PYTHON_EXE" ]; then
    echo "[ERROR] Unable to resolve the Python executable for conda environment '$ENV_NAME'." >&2
    exit 1
fi

export HOST=$BIND_HOST
export PORT=$BACKEND_PORT

echo "[INFO] Python: $PYTHON_EXE"
exec "$PYTHON_EXE" main.py
