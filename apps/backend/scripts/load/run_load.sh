#!/usr/bin/env bash
# Isolated load test: second backend + second engine (dummy LLM/TTS) on their own ports, Redis DB and folders.
# Never touches the running services. Needs: WORK BACKEND_DIR BACKEND_PY ENGINE_DIR ENGINE_PY TOOLS CHANNELS SECONDS
set -u
: "${WORK:?}" "${BACKEND_DIR:?}" "${BACKEND_PY:?}" "${ENGINE_DIR:?}" "${ENGINE_PY:?}" "${TOOLS:?}"
CHANNELS=${CHANNELS:-10}
SECONDS_TO_RUN=${SECONDS_TO_RUN:-600}
FORMATS=${FORMATS:-talk,music}
REDIS_DB=${REDIS_DB:-5}
BACKEND_PORT=${BACKEND_PORT:-3900}
ENGINE_PORT=${ENGINE_PORT:-8900}
REDIS_URL="redis://127.0.0.1:6379/$REDIS_DB"

mkdir -p "$WORK/hls" "$WORK/audio" "$WORK/out"
ids=$(for i in $(seq 1 "$CHANNELS"); do printf 'lt%02d,' "$i"; done)

cd "$ENGINE_DIR" || exit 1
"$ENGINE_PY" "$TOOLS/make_config.py" "$WORK" "$ENGINE_PORT" "$REDIS_URL" || exit 1
ONAIR_REDIS_URL="$REDIS_URL" PYTHONUNBUFFERED=1 "$ENGINE_PY" -c 'from onair_engine.main import cli; cli()' \
    --config "$WORK/engine.yaml" --serve --transport redis --llm dummy --tts dummy --embedder dummy \
    > "$WORK/engine.log" 2>&1 &
engine_pid=$!

cd "$BACKEND_DIR" || exit 1
HOST=127.0.0.1 PORT=$BACKEND_PORT ONAIR_REDIS_URL="$REDIS_URL" REDIS_URL="$REDIS_URL" \
    ONAIR_STATION_IDS="${ids%,}" ONAIR_HLS_DIR="$WORK/hls" ONAIR_AUDIO_DIR="$WORK/audio" \
    ONAIR_ENGINE_API_URL="http://127.0.0.1:$ENGINE_PORT" ONAIR_ADMIN_PINS= PYTHONUNBUFFERED=1 \
    "$BACKEND_PY" -m app > "$WORK/backend.log" 2>&1 &
backend_pid=$!

cleanup() {
    kill "$backend_pid" "$engine_pid" 2>/dev/null
    sleep 3
    kill -9 "$backend_pid" "$engine_pid" 2>/dev/null
    wait 2>/dev/null
}
trap cleanup EXIT

for _ in $(seq 1 60); do
    "$BACKEND_PY" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$BACKEND_PORT/health', timeout=2)" 2>/dev/null && break
    sleep 1
done
"$BACKEND_PY" "$TOOLS/loadtest.py" "http://127.0.0.1:$BACKEND_PORT" "$CHANNELS" "$SECONDS_TO_RUN" "$WORK/out" \
    --watch "$backend_pid,$engine_pid" --formats "$FORMATS"
status=$?
sleep 8  # let the stop requests clean up
echo "backend_pid=$backend_pid engine_pid=$engine_pid exit=$status"
exit $status
