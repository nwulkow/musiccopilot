#!/usr/bin/env bash
# Start everything Scriptum needs and print where to point a browser.
#
#   ./start.sh                 build the client if stale, serve it on :9000
#   ./start.sh --dev           also run Vite on :5173 with the API proxied
#   ./start.sh --host 0.0.0.0  reachable from a phone on the same network
#   ./start.sh --port 9100     serve the API somewhere else
#
# The mic modes record on the machine running this, so run it on the laptop
# that is in the room (see scriptum/__main__.py).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT="${SCRIPTUM_PORT:-9000}"
HOST="${SCRIPTUM_HOST:-127.0.0.1}"
DEV=0
RELOAD=""
EXTRA=()

while [ $# -gt 0 ]; do
  case "$1" in
    --dev)     DEV=1; shift ;;
    --reload)  RELOAD="--reload"; shift ;;
    --port)    PORT="$2"; shift 2 ;;
    --host)    HOST="$2"; shift 2 ;;
    --help|-h) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         EXTRA+=("$1"); shift ;;
  esac
done

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "  no venv at .venv - create it with: uv venv --python 3.11 && uv pip install -r requirements.txt" >&2
  exit 1
fi

# config.gemini_api_key() only reads the environment; nothing loads .env itself.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# The Vite dev proxy and this script must agree on where the API lives.
export SCRIPTUM_API_PORT="$PORT"

need_build() {
  [ ! -f web/dist/index.html ] && return 0
  [ -n "$(find web/src web/index.html web/vite.config.js web/package.json \
            -newer web/dist/index.html 2>/dev/null | head -1)" ]
}

if [ ! -d web/node_modules ]; then
  echo "  installing web dependencies..."
  (cd web && npm install --silent)
fi

if [ "$DEV" -eq 0 ] && need_build; then
  echo "  building the client (web/dist is missing or stale)..."
  (cd web && npm run build --silent)
fi

VITE_PID=""
API_PID=""
cleanup() {
  [ -n "$API_PID" ]  && kill "$API_PID"  2>/dev/null
  [ -n "$VITE_PID" ] && kill "$VITE_PID" 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM

if [ "$DEV" -eq 1 ]; then
  # `exec` so VITE_PID is node itself: killing the `npm run dev` wrapper leaves
  # the real Vite process holding :5173. Its output is left visible so a
  # failure to start is not silent.
  (cd web && exec node_modules/.bin/vite --host "$HOST") &
  VITE_PID=$!
fi

# 0.0.0.0 is not an address you can open; show the one a phone would use.
lan_ip() {
  for i in en0 en1 en2; do
    ip="$(ipconfig getifaddr "$i" 2>/dev/null || true)"
    [ -n "$ip" ] && { echo "$ip"; return; }
  done
}
SHOWN="$HOST"
[ "$HOST" = "0.0.0.0" ] && SHOWN="$(lan_ip || true)" && SHOWN="${SHOWN:-127.0.0.1}"

echo
echo "  Scriptum"
echo "    app + api   http://$SHOWN:$PORT"
echo "    api docs    http://$SHOWN:$PORT/docs"
[ "$DEV" -eq 1 ] && echo "    vite (dev)  http://$SHOWN:5173   <- use this one while editing web/"
[ "$HOST" = "0.0.0.0" ] && echo "    (listening on 0.0.0.0 - other devices on this network can reach it)"
echo "    library     $ROOT"
echo "    ctrl-c to stop"
echo

# With Vite up the server runs in the background and we `wait` on it, rather
# than exec'ing it: exec would replace this shell and the trap that stops Vite
# would never run, and a *foreground* child would make bash defer the trap
# until it exited - so a `kill` aimed at this script would leave both alive.
# `wait` is interruptible, so cleanup actually gets to run.
if [ "$DEV" -eq 1 ]; then
  "$PY" -m scriptum --host "$HOST" --port "$PORT" $RELOAD "${EXTRA[@]+"${EXTRA[@]}"}" &
  API_PID=$!
  wait "$API_PID"
else
  exec "$PY" -m scriptum --host "$HOST" --port "$PORT" $RELOAD "${EXTRA[@]+"${EXTRA[@]}"}"
fi
