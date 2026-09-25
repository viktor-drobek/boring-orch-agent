#!/usr/bin/env bash
# Start (or reuse) a loopback coddy serve and run one native job driver, both detached
# with setsid so they survive the shell, terminal or agent session that launched them.
#
#   run_job.sh launch JOB.json PROMPT.md      start a new run
#   run_job.sh attach JOB.json SESSION_ID     supervise an existing session
#   run_job.sh status JOB.json                print state.json and liveness
#   run_job.sh stop-serve                     stop the coddy serve this script started
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${CODDY_DRIVER_STATE:-$HOME/.local/state/coddy-job-driver}"
PORT="${CODDY_DRIVER_PORT:-12345}"
BASE="http://127.0.0.1:$PORT"
TOKEN_FILE="$STATE_DIR/http-token"
SERVE_PID="$STATE_DIR/serve.pid"
ISOLATED_CONFIG="$STATE_DIR/isolated-config.yaml"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

serve_alive() {
  [[ -s "$SERVE_PID" ]] && kill -0 "$(cat "$SERVE_PID")" 2>/dev/null
}

prepare_isolated_config() {
  PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" python3 - "$ISOLATED_CONFIG" <<'PY'
from pathlib import Path
import sys
from isolated_config import primary_config_path, write_isolated_config

write_isolated_config(primary_config_path(), Path(sys.argv[1]))
PY
}

ensure_serve() {
  if [[ ! -s "$TOKEN_FILE" ]]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE")
  fi
  if serve_alive; then
    return
  fi
  if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$PORT\b"; then
    echo "port $PORT is taken by a server this script did not start; set CODDY_DRIVER_PORT" >&2
    exit 1
  fi
  prepare_isolated_config
  # The token goes through the environment, never the command line.
  CODDY_HTTP_TOKEN="$(cat "$TOKEN_FILE")" CODDY_CONFIG="$ISOLATED_CONFIG" setsid nohup coddy serve --http --host 127.0.0.1 --port "$PORT" \
    --config "$ISOLATED_CONFIG" \
    >> "$STATE_DIR/serve.log" 2>&1 < /dev/null &
  echo $! > "$SERVE_PID"
  for _ in $(seq 60); do
    if curl -s -o /dev/null -H "Authorization: Bearer $(cat "$TOKEN_FILE")" "$BASE/v1/models"; then
      echo "coddy serve up on $BASE (pid $(cat "$SERVE_PID"), log $STATE_DIR/serve.log)"
      return
    fi
    sleep 1
  done
  echo "coddy serve did not come up; see $STATE_DIR/serve.log" >&2
  exit 1
}

run_dir() {
  python3 -c 'import json,sys,pathlib; p=pathlib.Path(sys.argv[1]).resolve(); print(p.parent / json.loads(p.read_text())["id"])' "$1"
}

cmd="${1:-}"
case "$cmd" in
  launch|attach)
    [[ $# -eq 3 ]] || { echo "usage: $0 $cmd JOB.json ${cmd/launch/PROMPT.md}" >&2; exit 2; }
    job="$(realpath "$2")"
    ensure_serve
    out="$(run_dir "$job")"
    mkdir -p "$out"
    if [[ "$cmd" == launch ]]; then extra=(--prompt-file "$(realpath "$3")"); else extra=(--session "$3"); fi
    setsid nohup python3 "$HERE/driver.py" "$cmd" --job "$job" --token-file "$TOKEN_FILE" \
      --base "$BASE" "${extra[@]}" >> "$out/driver.out" 2>&1 < /dev/null &
    echo "driver started (pid $!); follow $out/driver.out, state in $out/state.json"
    ;;
  status)
    [[ $# -eq 2 ]] || { echo "usage: $0 status JOB.json" >&2; exit 2; }
    python3 "$HERE/driver.py" status --out "$(run_dir "$(realpath "$2")")"
    echo "serve alive: $(serve_alive && echo yes || echo no)"
    ;;
  stop-serve)
    if serve_alive; then kill "$(cat "$SERVE_PID")" && echo "stopped coddy serve $(cat "$SERVE_PID")"; else echo "not running"; fi
    ;;
  *)
    sed -n '2,9p' "$0" >&2
    exit 2
    ;;
esac
