#!/usr/bin/env bash
# 一键启动 AIAMC Harness 双模式监控台并自动打开浏览器。
# 用法：
#   ./start_console.sh            # 启动（若已运行则直接复用）并打开浏览器
#   ./start_console.sh --stop     # 停止后台控制台进程
#   ./start_console.sh --fg       # 前台运行（Ctrl+C 退出）
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DIR/../.." && pwd)"
PORT="${AIAMC_CONSOLE_PORT:-8795}"
HOST="127.0.0.1"
URL="http://${HOST}:${PORT}/"
PID_FILE="$REPO_ROOT/code/mcagent/logs/.agent_console_${PORT}.pid"
LOG_FILE="$REPO_ROOT/code/mcagent/logs/.agent_console_${PORT}.log"

open_url() {
  if command -v kde-open >/dev/null 2>&1; then
    setsid -f kde-open "$1" >/dev/null 2>&1 || nohup kde-open "$1" >/dev/null 2>&1 &
  elif command -v xdg-open >/dev/null 2>&1; then
    setsid -f xdg-open "$1" >/dev/null 2>&1 || nohup xdg-open "$1" >/dev/null 2>&1 &
  elif command -v wslview >/dev/null 2>&1; then
    wslview "$1" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$1" >/dev/null 2>&1 &
  else
    python3 -m webbrowser -t "$1" >/dev/null 2>&1 &
  fi
  sleep 0.25
}

is_ready() {
  python3 - "$HOST" "$PORT" <<'PY'
import sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(f"http://{sys.argv[1]}:{sys.argv[2]}/healthz", timeout=1.5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

if [[ "${1:-}" == "--stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
      kill "$PID" 2>/dev/null || true
      echo "[agent-console] 已停止后台服务 (PID $PID)"
    fi
    rm -f "$PID_FILE"
  else
    echo "[agent-console] 未发现后台 PID 记录"
  fi
  exit 0
fi

PYTHON_BIN="python3"
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
fi

if [[ "${1:-}" == "--fg" ]]; then
  exec "$PYTHON_BIN" "$DIR/server.py" --host "$HOST" --port "$PORT" --open
fi

mkdir -p "$(dirname "$PID_FILE")"

if is_ready; then
  echo "[agent-console] 控制台已在运行 -> $URL"
  open_url "$URL"
  exit 0
fi

nohup "$PYTHON_BIN" "$DIR/server.py" --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 </dev/null &
SERVER_PID=$!
echo "$SERVER_PID" >"$PID_FILE"

for _ in $(seq 1 30); do
  if is_ready; then
    echo "[agent-console] 启动成功 (PID $SERVER_PID) -> $URL"
    echo "[agent-console] 停止命令: $DIR/start_console.sh --stop"
    open_url "$URL"
    exit 0
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[agent-console] 启动失败，最近日志：" >&2
    tail -n 30 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    exit 1
  fi
  sleep 0.2
done

echo "[agent-console] 等待超时，请检查日志: $LOG_FILE" >&2
exit 1
