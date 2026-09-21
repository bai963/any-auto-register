#!/bin/sh
# Linux backend stop script. Compatible with sh, bash and zsh.

set -eu

BACKEND_PORT=${BACKEND_PORT:-8000}
SOLVER_PORT=${SOLVER_PORT:-8889}
CLIPROXYAPI_PORT=${CLIPROXYAPI_PORT:-8317}
FULL_STOP=${FULL_STOP:-1}

for port in "$BACKEND_PORT" "$SOLVER_PORT" "$CLIPROXYAPI_PORT"; do
    case $port in
        ''|*[!0-9]*) echo "[ERROR] Port must be a valid number: $port" >&2; exit 1 ;;
    esac
done

pids_for_port() {
    port=$1

    if command -v lsof >/dev/null 2>&1; then
        lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
        return
    fi

    if command -v fuser >/dev/null 2>&1; then
        fuser -n tcp "$port" 2>/dev/null || true
        return
    fi

    if command -v ss >/dev/null 2>&1; then
        ss -ltnp "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' || true
        return
    fi

    echo "[ERROR] Cannot inspect listening ports: install lsof, psmisc (fuser), or iproute2 (ss)." >&2
    return 1
}

stop_pid() {
    pid=$1

    if ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi

    echo "[INFO] Attempting graceful stop for PID=$pid"
    kill -TERM "$pid" 2>/dev/null || {
        echo "[WARN] Unable to stop PID=$pid (insufficient permission?)" >&2
        return 1
    }

    attempts=0
    while [ "$attempts" -lt 6 ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "[OK] Stopped PID=$pid"
            return 0
        fi
        sleep 1
        attempts=$((attempts + 1))
    done

    echo "[WARN] PID=$pid did not exit in time; force stopping it"
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "[OK] Force-stopped PID=$pid"
        return 0
    fi

    echo "[WARN] Failed to stop PID=$pid" >&2
    return 1
}

ports="$BACKEND_PORT $SOLVER_PORT"
if [ "$FULL_STOP" != "0" ]; then
    ports="$ports $CLIPROXYAPI_PORT"
fi

echo "[INFO] Preparing to stop services on ports: $ports"
targets=''
for port in $ports; do
    for pid in $(pids_for_port "$port"); do
        case " $targets " in
            *" $pid "*) ;;
            *) targets="$targets $pid" ;;
        esac
    done
done

if [ -z "$targets" ]; then
    echo "[INFO] No processes to stop"
    exit 0
fi

status=0
for pid in $targets; do
    stop_pid "$pid" || status=1
done

echo "[INFO] Stop complete"
exit "$status"
