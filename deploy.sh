#!/usr/bin/env bash
# onAIr VM 배포 스크립트 — 저장소 루트에서 `bash deploy.sh`
#
# 1. git pull (main)
# 2. 바뀐 부분만 의존성 설치 / 프론트 빌드
# 3. Redis 컨테이너 기동 확인
# 4. systemd 서비스 재시작
#
# 옵션:
#   --full   변경 여부와 상관없이 전부 설치·빌드
#
# 변경 판단은 "마지막으로 배포에 성공한 커밋"(.git/onair-deployed) 기준입니다.
# 직접 git pull을 했거나 이전 배포가 중간에 실패해도 아직 배포 안 된 변경은 다시 설치·빌드됩니다.
#   --no-pull  pull 없이 설치·빌드·재시작만
#
# 서비스 이름은 환경변수로 바꿀 수 있습니다 (없는 서비스는 건너뜀):
#   SERVICES="onair-backend onair-engine" bash deploy.sh
# 프론트 정적 파일을 서빙하는 경로(Caddy root)도 바꿀 수 있습니다:
#   WEB_ROOT=/var/www/onair bash deploy.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCH="${BRANCH:-main}"
SERVICES="${SERVICES:-onair-backend onair-engine}"
WEB_ROOT="${WEB_ROOT:-/var/www/onair}"

FULL=0
PULL=1
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --no-pull) PULL=0 ;;
    *) echo "알 수 없는 옵션: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }

cd "$ROOT"

# 마지막으로 배포에 성공한 커밋 (git이 추적하지 않는 .git 안에 기록)
STATE_FILE="$(git rev-parse --git-dir)/onair-deployed"

# ---------------------------------------------------------------- 1. pull
if [[ $PULL -eq 1 ]]; then
  log "git pull origin $BRANCH"
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    warn "VM에 커밋 안 된 수정이 있습니다. pull이 실패하면 'git status'로 확인하세요."
  fi
  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"
fi
NEW_HEAD="$(git rev-parse HEAD)"

LAST_DEPLOYED="$(cat "$STATE_FILE" 2>/dev/null || true)"
if [[ -z "$LAST_DEPLOYED" ]] || ! git cat-file -e "$LAST_DEPLOYED^{commit}" 2>/dev/null; then
  echo "배포 기록 없음 — 전부 설치·빌드합니다"
  FULL=1
  CHANGED=""
elif [[ "$LAST_DEPLOYED" == "$NEW_HEAD" ]]; then
  echo "마지막 배포 이후 새 커밋 없음 ($NEW_HEAD)"
  CHANGED=""
else
  echo "마지막 배포 $LAST_DEPLOYED -> $NEW_HEAD"
  CHANGED="$(git diff --name-only "$LAST_DEPLOYED" "$NEW_HEAD")"
fi

# 경로 패턴(정규식)에 해당하는 파일이 바뀌었거나 --full이면 true
changed() { [[ $FULL -eq 1 ]] || grep -qE "$1" <<<"$CHANGED"; }

# ---------------------------------------------------------------- 2. 의존성/빌드
ensure_venv() {
  local dir="$1"
  if [[ ! -x "$dir/.venv/bin/python" ]]; then
    log "$dir: venv 생성"
    python3 -m venv "$dir/.venv"
    FULL_FOR_NEW_VENV=1
  fi
}

# 백엔드
FULL_FOR_NEW_VENV=0
ensure_venv apps/backend
if [[ $FULL_FOR_NEW_VENV -eq 1 ]] || changed '^(apps/backend/pyproject\.toml|packages/onair_schema/)'; then
  log "backend: pip install"
  apps/backend/.venv/bin/python -m pip install -q -e packages/onair_schema -e apps/backend
fi

# 엔진
FULL_FOR_NEW_VENV=0
ensure_venv apps/engine
if [[ $FULL_FOR_NEW_VENV -eq 1 ]] || changed '^(apps/engine/pyproject\.toml|packages/onair_schema/|deploy\.sh$)'; then
  log "engine: pip install"
  apps/engine/.venv/bin/python -m pip install -q -e packages/onair_schema -e "apps/engine[redis,api]"
fi

# 프론트엔드 (VITE_* 환경변수는 빌드 시점에 박히므로 .env 수정 후엔 --full)
if [[ ! -d apps/frontend/node_modules ]] || changed '^apps/frontend/package(-lock)?\.json$'; then
  log "frontend: npm ci"
  (cd apps/frontend && npm ci)
fi
if [[ ! -d apps/frontend/dist ]] || changed '^apps/frontend/'; then
  log "frontend: npm run build"
  (cd apps/frontend && npm run build)
fi

# Caddy는 레포의 dist가 아니라 WEB_ROOT를 서빙하므로 매번 동기화 (변경분만 복사됨)
log "frontend: dist -> $WEB_ROOT"
sudo mkdir -p "$WEB_ROOT"
sudo rsync -a --delete apps/frontend/dist/ "$WEB_ROOT/"

# .env 누락 경고 (git에 없으므로 pull로 생기지 않음)
for d in apps/backend apps/engine apps/frontend; do
  [[ -f "$d/.env" ]] || warn "$d/.env 없음 — $d/.env.example 참고해서 만들어주세요."
done

# ---------------------------------------------------------------- 3. Redis
if command -v docker >/dev/null 2>&1; then
  log "redis: docker compose up -d"
  docker compose up -d redis
else
  warn "docker 없음 — Redis 기동 확인 생략"
fi

# ---------------------------------------------------------------- 4. 재시작
log "서비스 재시작"
restarted=0
for svc in $SERVICES; do
  if systemctl list-unit-files "$svc.service" --no-legend 2>/dev/null | grep -q "$svc"; then
    sudo systemctl restart "$svc"
    if systemctl is-active --quiet "$svc"; then
      echo "  $svc: active"
    else
      warn "$svc 기동 실패 — journalctl -u $svc -n 50"
    fi
    restarted=1
  else
    warn "$svc.service 없음 — 건너뜀"
  fi
done
if [[ $restarted -eq 0 ]]; then
  warn "재시작한 서비스가 없습니다. 직접 띄운 프로세스라면 수동으로 재시작하세요."
fi

# 설치·빌드가 끝까지 성공했을 때만 기록 (set -e라 중간 실패 시 여기까지 오지 않음)
echo "$NEW_HEAD" >"$STATE_FILE"
log "배포 완료 ($(git rev-parse --short HEAD))"
