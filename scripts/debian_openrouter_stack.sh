#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
DATA_DIR="${NANOBOT_DATA_DIR:-$HOME/.nanobot}"
CONFIG_PATH="${NANOBOT_CONFIG_PATH:-$DATA_DIR/config.json}"
ENV_FILE="${OPENROUTER_PROXY_ENV_FILE:-$DATA_DIR/openrouter-proxy.env}"
RUN_DIR="$DATA_DIR/run"
LOG_DIR="$DATA_DIR/logs"
PROXY_PID_FILE="$RUN_DIR/openrouter-proxy.pid"
GATEWAY_PID_FILE="$RUN_DIR/nanobot-gateway.pid"
PROXY_LOG="$LOG_DIR/openrouter-proxy.log"
GATEWAY_LOG="$LOG_DIR/nanobot-gateway.log"
PROXY_HOST="${PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${PROXY_PORT:-8088}"
GATEWAY_PORT="${GATEWAY_PORT:-18790}"
NANOBOT_MODEL="${NANOBOT_MODEL:-anthropic/claude-sonnet-4-5}"

usage() {
  cat <<EOF
Usage: $(basename "$0") <install|start|stop|restart|status|logs>

Commands:
  install   Create venv, install nanobot, create env/config scaffolding
  start     Start the OpenRouter proxy and nanobot gateway in background
  stop      Stop the proxy and gateway
  restart   Restart both services
  status    Show whether the proxy and gateway are running
  logs      Tail proxy and gateway logs

Important:
  1. Put your real OpenRouter key in: $ENV_FILE
  2. The script configures nanobot to use a dummy OpenRouter key and the local proxy.
EOF
}

ensure_dirs() {
  mkdir -p "$DATA_DIR" "$RUN_DIR" "$LOG_DIR"
}

venv_python() {
  echo "$VENV_DIR/bin/python"
}

venv_nanobot() {
  echo "$VENV_DIR/bin/nanobot"
}

require_venv() {
  if [[ ! -x "$(venv_python)" ]]; then
    echo "Virtualenv not found at $VENV_DIR. Run: $0 install" >&2
    exit 1
  fi
}

ensure_env_file() {
  ensure_dirs
  if [[ ! -f "$ENV_FILE" ]]; then
    cat >"$ENV_FILE" <<EOF
# Real OpenRouter key used only by the local proxy.
OPENROUTER_API_KEY=

# Optional overrides:
# PROXY_HOST=127.0.0.1
# PROXY_PORT=8088
# GATEWAY_PORT=18790
# NANOBOT_MODEL=anthropic/claude-sonnet-4-5
EOF
    chmod 600 "$ENV_FILE"
    echo "Created $ENV_FILE"
    echo "Fill in OPENROUTER_API_KEY before running start."
  fi
}

install_stack() {
  ensure_dirs

  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required. On Debian: sudo apt-get install -y python3 python3-venv" >&2
    exit 1
  fi

  if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
  fi

  "$(venv_python)" -m pip install --upgrade pip
  "$(venv_python)" -m pip install -e "$ROOT_DIR"

  ensure_env_file
  configure_nanobot

  cat <<EOF
Install complete.

Next:
  1. Edit $ENV_FILE and set OPENROUTER_API_KEY
  2. Start services: $0 start
  3. Check logs: $0 logs
EOF
}

configure_nanobot() {
  require_venv
  ensure_dirs

  CONFIG_PATH="$CONFIG_PATH" \
  PROXY_HOST="$PROXY_HOST" \
  PROXY_PORT="$PROXY_PORT" \
  NANOBOT_MODEL="$NANOBOT_MODEL" \
  "$(venv_python)" <<'PY'
import json
import os
from pathlib import Path

config_path = Path(os.environ["CONFIG_PATH"]).expanduser()
proxy_host = os.environ["PROXY_HOST"]
proxy_port = os.environ["PROXY_PORT"]
model = os.environ["NANOBOT_MODEL"]

config_path.parent.mkdir(parents=True, exist_ok=True)
if config_path.exists():
    data = json.loads(config_path.read_text(encoding="utf-8"))
else:
    data = {}

data.setdefault("agents", {}).setdefault("defaults", {})
data.setdefault("providers", {}).setdefault("openrouter", {})

data["agents"]["defaults"]["provider"] = "openrouter"
data["agents"]["defaults"]["model"] = model
data["providers"]["openrouter"]["apiKey"] = "dummy"
data["providers"]["openrouter"]["apiBase"] = f"http://{proxy_host}:{proxy_port}/v1"

config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Configured nanobot at {config_path}")
PY
}

load_env() {
  ensure_env_file
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  export PROXY_HOST PROXY_PORT GATEWAY_PORT NANOBOT_MODEL OPENROUTER_API_KEY
}

is_pid_running() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

start_proxy() {
  if is_pid_running "$PROXY_PID_FILE"; then
    echo "Proxy already running (pid $(cat "$PROXY_PID_FILE"))"
    return
  fi

  if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
    echo "OPENROUTER_API_KEY is empty in $ENV_FILE" >&2
    exit 1
  fi

  nohup "$(venv_nanobot)" proxy openrouter \
    --host "$PROXY_HOST" \
    --port "$PROXY_PORT" \
    >>"$PROXY_LOG" 2>&1 &
  echo $! >"$PROXY_PID_FILE"
  echo "Started proxy (pid $(cat "$PROXY_PID_FILE"))"
}

start_gateway() {
  if is_pid_running "$GATEWAY_PID_FILE"; then
    echo "Gateway already running (pid $(cat "$GATEWAY_PID_FILE"))"
    return
  fi

  nohup "$(venv_nanobot)" gateway \
    --port "$GATEWAY_PORT" \
    --config "$CONFIG_PATH" \
    >>"$GATEWAY_LOG" 2>&1 &
  echo $! >"$GATEWAY_PID_FILE"
  echo "Started gateway (pid $(cat "$GATEWAY_PID_FILE"))"
}

start_stack() {
  require_venv
  load_env
  configure_nanobot
  start_proxy
  sleep 2
  start_gateway
}

stop_one() {
  local name="$1"
  local pid_file="$2"
  if is_pid_running "$pid_file"; then
    local pid
    pid="$(cat "$pid_file")"
    kill "$pid" 2>/dev/null || true
    rm -f "$pid_file"
    echo "Stopped $name"
  else
    rm -f "$pid_file"
    echo "$name is not running"
  fi
}

stop_stack() {
  stop_one "gateway" "$GATEWAY_PID_FILE"
  stop_one "proxy" "$PROXY_PID_FILE"
}

status_stack() {
  if is_pid_running "$PROXY_PID_FILE"; then
    echo "proxy: running (pid $(cat "$PROXY_PID_FILE"))"
  else
    echo "proxy: stopped"
  fi

  if is_pid_running "$GATEWAY_PID_FILE"; then
    echo "gateway: running (pid $(cat "$GATEWAY_PID_FILE"))"
  else
    echo "gateway: stopped"
  fi

  echo "config: $CONFIG_PATH"
  echo "env:    $ENV_FILE"
  echo "proxy:  http://$PROXY_HOST:$PROXY_PORT"
}

logs_stack() {
  ensure_dirs
  touch "$PROXY_LOG" "$GATEWAY_LOG"
  tail -n 50 -f "$PROXY_LOG" "$GATEWAY_LOG"
}

cmd="${1:-}"
case "$cmd" in
  install)
    install_stack
    ;;
  start)
    start_stack
    ;;
  stop)
    stop_stack
    ;;
  restart)
    stop_stack
    start_stack
    ;;
  status)
    status_stack
    ;;
  logs)
    logs_stack
    ;;
  *)
    usage
    exit 1
    ;;
esac
