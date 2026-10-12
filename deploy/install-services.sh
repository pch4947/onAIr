#!/usr/bin/env bash
# deploy/systemd/*.service를 이 VM 경로·유저로 채워 /etc/systemd/system에 설치하고 켠다.
# 최초 1회 (유닛 파일이 바뀌었을 때도 다시 실행): bash deploy/install-services.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${RUN_USER:-$(id -un)}"

for unit in "$ROOT"/deploy/systemd/*.service; do
  name="$(basename "$unit")"
  echo "install $name (user=$RUN_USER, root=$ROOT)"
  sed -e "s|__ROOT__|$ROOT|g" -e "s|__USER__|$RUN_USER|g" "$unit" \
    | sudo tee "/etc/systemd/system/$name" >/dev/null
done

sudo systemctl daemon-reload
sudo systemctl enable onair-backend onair-engine
echo "설치 완료. 처음이면 'bash deploy.sh --full'로 설치·빌드·기동하세요."
echo "로그: journalctl -u onair-backend -f / journalctl -u onair-engine -f"
