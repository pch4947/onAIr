#!/usr/bin/env bash
# Render 테스트 배포 — 엔진과 백엔드를 한 서비스에서 실행한다.
# 백엔드는 엔진이 쓴 audio_ref 파일을 같은 디스크에서 읽는다 (docs/ENGINE_REDIS_CONTRACT.md 5장).
# Render는 서비스끼리 디스크를 공유하지 않으므로 한 서비스로 묶는다. 최종 시연(VM 한 대)과 같은 구조다.
# 설정값: apps/engine/README.md "Render 테스트 배포"
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENGINE_DIR="$ROOT/apps/engine"
BACKEND_DIR="$ROOT/apps/backend"
PORT="${PORT:-3000}"

# 백엔드를 비편집 설치하면 기본 경로가 site-packages 기준이 되므로 절대 경로로 고정한다.
# 엔진은 설정의 transport.audio_dir(엔진 폴더 기준 var/audio)에 쓴다
export ONAIR_AUDIO_DIR="${ONAIR_AUDIO_DIR:-$ENGINE_DIR/var/audio}"
export ONAIR_HLS_DIR="${ONAIR_HLS_DIR:-$BACKEND_DIR/var/hls}"
# 엔진은 ONAIR_REDIS_URL만 읽고, 백엔드는 ONAIR_REDIS_URL을 REDIS_URL보다 우선한다
export ONAIR_REDIS_URL="${ONAIR_REDIS_URL:-${REDIS_URL:?REDIS_URL 또는 ONAIR_REDIS_URL을 설정하세요}}"
mkdir -p "$ONAIR_AUDIO_DIR"

# 종료 신호가 오면 엔진 재시작 루프와 그 자식까지 프로세스 그룹째 정리한다
trap 'trap - TERM INT EXIT; kill 0 2>/dev/null' TERM INT EXIT

(cd "$BACKEND_DIR" && exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --workers 1) &
BACKEND_PID=$!

# 백엔드 소비 그룹은 0-0부터 읽어 순서가 바뀌어도 유실은 없지만, 로그를 읽기 쉽게 백엔드가 뜬 뒤 시작한다
for _ in $(seq 60); do
  python -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)" \
    "http://127.0.0.1:$PORT/health" 2>/dev/null && break
  sleep 1
done

(
  cd "$ENGINE_DIR"
  while true; do
    # ONAIR_ENGINE_ARGS는 인자 여러 개이므로 따옴표 없이 펼친다
    # shellcheck disable=SC2086
    onair-engine --config "${ONAIR_ENGINE_CONFIG:-config/station.render.yaml}" \
      ${ONAIR_ENGINE_ARGS:-} || true
    echo "ENGINE_EXITED — 5초 뒤 다시 시작합니다" >&2
    sleep 5
  done
) &

# 백엔드가 죽으면 스크립트도 끝나 Render가 서비스를 재시작한다. 엔진은 위 루프가 되살린다
wait "$BACKEND_PID"
