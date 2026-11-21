# 배포 (VM)

| 파일 | 하는 일 | 언제 |
| --- | --- | --- |
| `../deploy.sh` | git pull → 바뀐 부분만 설치·빌드 → 서비스 재시작 | 배포할 때마다 (저장소 루트에서 `bash deploy.sh`) |
| `install-services.sh` | `systemd/*.service`를 이 VM 경로·유저로 설치 | 처음 한 번, 유닛 파일이 바뀌었을 때 |
| `Caddyfile` | 도메인 → 백엔드·프론트 연결 | 처음 한 번, 이 파일이 바뀌었을 때 |

`deploy.sh`는 Caddy 설정을 건드리지 않는다. `Caddyfile`이 바뀐 배포에서는 아래를 따로 실행한다.

## Caddy 설정 적용

저장소 루트(`~/onAIr`)에서:

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak          # 되돌릴 수 있게
diff /etc/caddy/Caddyfile deploy/Caddyfile                       # 바뀌는 부분 확인
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile # Valid configuration
sudo systemctl reload caddy                                      # 끊지 않고 설정만 다시 읽는다
```

되돌리기: `sudo cp /etc/caddy/Caddyfile.bak /etc/caddy/Caddyfile && sudo systemctl reload caddy`

## 백엔드에 새 경로를 만들 때

Caddy는 `Caddyfile`에 적은 경로(`/api/*`, `/hls/*`, `/ws/*`, `/health*`)만 백엔드로 넘긴다.
새 앞부분(예: `/ws`)을 만들면 `Caddyfile`에 `handle` 블록을 추가하고 위 순서로 적용한다.
빠지면 그 요청은 프론트의 `index.html`을 받는다 — #312의 웹소켓이 이렇게 배포 뒤에 붙지 않았다(2026-11-22에 `/ws`를 추가해 고침).

## 확인

```bash
# 백엔드 직접, 그리고 도메인(Caddy) 경유로 웹소켓 전환이 되는지 — 둘 다 101
for u in http://127.0.0.1:3000 https://3-37-161-202.sslip.io; do
  curl -s --http1.1 -o /dev/null -w "$u %{http_code}\n" --max-time 3 \
    -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
    -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" "$u/ws/stations/st_ch01"
done
```

- `--http1.1`을 빼면 curl이 https에서 HTTP/2로 붙어 전환 헤더가 무시되고 404가 나온다. 브라우저는 상관없다.
- 백엔드 설정은 `apps/backend/.env`이고, 코드 기본값보다 우선한다. 기본값을 바꾸는 PR(예: #359의 `ONAIR_BROADCAST_DELAY_SECONDS`)은 VM `.env`도 같은 배포에서 확인한다.
