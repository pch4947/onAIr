# onAIr Backend — FastAPI / Redis / HLS

FastAPI가 엔진의 Redis 이벤트를 받아 방송 큐에 저장하고, FFmpeg로 HLS를 생성합니다.
`/health`는 서버 프로세스 응답 여부이며 방송 준비 완료를 의미하지 않습니다.

## 실행 (Windows PowerShell)

Python 3.12 이상에서, 저장소 루트 기준으로 한 단계씩 실행합니다.

1. `cd apps/backend`
2. `python -m venv .venv`
3. `.\.venv\Scripts\python.exe -m pip install -e .`
4. `Copy-Item .env.example .env` (기존 .env가 있으면 생략)
5. `.\.venv\Scripts\python.exe -m app`

별도 PowerShell에서 `Invoke-RestMethod http://127.0.0.1:3000/health`로 확인합니다.
응답은 `{"ok":true,"service":"onAIr backend"}`입니다.
API 문서는 http://127.0.0.1:3000/docs 에서 확인합니다. 종료는 Ctrl+C입니다.

`python` 실행 시 버전이 출력되지 않거나 Microsoft Store가 열리면 실제 Python 실행 파일을 사용해야 합니다.
Codex에서 만든 .venv가 이미 있으면 5번부터 실행할 수 있습니다.
macOS/Linux에서는 가상환경 실행 파일이 `.venv/bin/python`입니다.

## 모듈 역할

- `app/main.py`: FastAPI 앱과 health 경로 정의
- `app/config.py`: 백엔드 폴더의 .env 로드, HOST/PORT 읽기 및 포트 검증
- `app/__main__.py`: 설정을 읽고 Uvicorn HTTP 서버 시작
- `pyproject.toml`: 엔진과 동일한 방식의 Python 버전 및 의존성 선언

기본값은 HOST=127.0.0.1, PORT=3000이며 OS 환경변수가 .env보다 우선합니다.
방송 소비자와 HLS 발행기는 한 서버 프로세스에서만 실행합니다.

## API와 현재 한계

| API | 동작 |
| --- | --- |
| GET /health | 서버 생존 확인 |
| GET /api/stream/state | 방송 상태, 대기 요청 수와 최장 대기시간 |
| GET /api/requests | 요청 목록 |
| POST /api/requests | prompt와 선택적 listenerId를 받아 202 반환 |
| POST /api/scheduler/tick | 버퍼 기준 정책 판단 및 방송 상태 갱신 |

Node 서버와 package.json은 제거했습니다. 실행은 `python -m app`입니다.
`app/api.py`는 입력 모델과 HTTP 경로, 앱별 메모리 상태를 관리합니다.
`app/scheduler.py`는 기존 버퍼 정책의 순수 계산 함수입니다.
환경변수 STREAM_BUFFER_TARGET_SECONDS(45), REQUEST_RESPONSE_WINDOW_SECONDS(90)를 유지합니다.

정상 요청의 기존 JSON 필드 및 상태코드를 유지합니다. 잘못된 JSON/필드 입력은
기존 400 대신 FastAPI 기본 422(detail 배열)로 응답합니다.
음수/무한대/NaN 버퍼와 1 미만 목표 시간은 거부합니다.
CORS는 개발용 모든 origin을 허용하며 인증 쿠키는 허용하지 않습니다.
CORS preflight는 middleware가 200으로 처리합니다.

요청 목록과 엔진 세그먼트 큐는 Redis에 저장됩니다. 반드시 worker 1개로 실행합니다.
tick은 정책 판단만 수행하며 요청을 소비하거나 오디오를 생성하지 않습니다.
엔진은 --transport redis로 실행합니다. 프론트엔드 오디오는 HLS를 사용하며 POST /api/segments는 없습니다.

## 요청 접수와 엔진 전달

`POST /api/requests`는 요청을 Redis에 저장하면서 엔진의 `request.arrived` 이벤트를 발행합니다.
기존 prompt/listenerId는 유지하고 kind는 story(기본) 또는 mood입니다. 초기 상태는 queued에서 requested로 변경했습니다.
엔진이 먼저 응답하면 POST에도 최신 상태가 반환될 수 있으며, 202 자체는 엔진 처리 완료를 뜻하지 않습니다.

```powershell
$body = @{ prompt = "차분한 분위기로 부탁해요"; kind = "mood" } | ConvertTo-Json
$key = [guid]::NewGuid().ToString() # 새 요청에만 새 키 생성. 재시도는 이 값을 재사용
$result = Invoke-RestMethod http://127.0.0.1:3000/api/requests -Method Post -ContentType 'application/json; charset=utf-8' -Headers @{ 'Idempotency-Key' = $key } -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod ("http://127.0.0.1:3000/api/requests/" + $result.request.id + "/state")
```

같은 키·같은 입력은 기존 요청을 반환하고 재발행하지 않습니다. 같은 키·다른 입력은 409, 잘못된 입력은 422, Redis 장애는 503입니다.
응답 유실·시간 초과 시에도 같은 키로 재시도합니다. 헤더를 생략하면 매번 새 요청입니다.
목록과 개별 상태는 동일한 엔진 최신 상태를 반영하며 백엔드 재시작 후에도 유지됩니다.
방송 상태의 요청 통계는 rejected/failed/played를 제외하며, Redis 장애 시 null 및 requestStatsAvailable=false가 됩니다.
상태 이력 및 MIXING/PUBLISHED/PLAYED 전이는 구현했습니다. 조회 목록·재시도 키의 자동 만료, 페이지네이션, 인증·본인 확인은 후속 작업입니다.
현재 엔진의 QUEUED 이벤트 발행 누락은 엔진 담당자와 조율해야 합니다. 프론트엔드는 요청 통계의 null과 새 초기 상태를 처리하도록 연동이 필요합니다.

## 회귀 검증

백엔드 폴더에서 `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`.
테스트는 별도 임시 포트의 실제 HTTP 서버를 사용하고 종료합니다.

전체 계획은 [전환 계획](../../docs/BACKEND_MIGRATION.md)을 참고하세요.

## Redis 연결 확인 단계 (진행 중)

`REDIS_URL` 기본값은 `redis://127.0.0.1:6379/0`입니다.
기존 .env가 있어도 이 변수를 생략하면 기본값을 사용합니다.
서버 수명주기에서 비동기 Redis 클라이언트를 만들고 종료 시 연결을 정리합니다.

- GET /health: 서버 생존 확인 (Redis 중단 시에도 200)
- GET /health/redis: 실제 PING 성공 시 200 및 `{"ok":true,"redis":"connected"}`
- Redis 연결 실패/시간 초과: 503 및 `{"ok":false,"redis":"unavailable"}`

연결 확인 요청은 최대 약 3초로 제한합니다. 에러 응답에 연결 URL이나 비밀번호를 노출하지 않습니다.
실제 Redis 서버가 필요하며 Python redis 패키지 설치만으로 Redis 서버가 설치되지는 않습니다.
Docker Desktop 및 WSL 환경에서 Redis 컨테이너 실행을 확인했습니다.
단위 테스트의 Redis 클라이언트는 mock이며 실제 서버 연결 성공을 뜻하지 않습니다.

연결 확인 이후 엔진 metadata 소비와 세그먼트 큐 영속화를 추가했습니다. 아래 소비자 절을 참고하세요.
Redis 연동 전체 완료 후 두 번째 기능 커밋으로 묶습니다.

### Docker로 로컬 Redis 실행

Docker Desktop 설치 및 엔진 시작 후 저장소 루트에서 한 단계씩 실행합니다.

1. `docker compose up -d redis`
2. `docker compose exec redis redis-cli ping` → `PONG` 확인
3. 백엔드 폴더에서 서버를 재시작하고 `/health/redis` 확인

compose.yaml은 Redis 7.4 Alpine 이미지와 데이터 볼륨을 사용하고,
호스트의 127.0.0.1:6379에만 포트를 공개합니다. 로컬 개발용 설정입니다.
중지는 `docker compose stop redis`, 재시작은 `docker compose start redis`입니다.
중지 시 `/health/redis`는 503, 재시작 후 200으로 회복되는지도 확인합니다.
볼륨 데이터는 컨테이너 중지 후에도 유지됩니다.

설치 참고: https://docs.docker.com/desktop/setup/install/windows-install/

### 실제 Redis 검증 결과

- redis-cli PING: PONG
- FastAPI /health/redis: 200 connected
- Redis 중지: /health/redis 503 unavailable, /health 200
- Redis 재시작: FastAPI 재시작 없이 /health/redis 200 복구
- 자동 테스트 6개 통과 (Redis 단위 테스트 mock 검증과 실제 Docker 검증은 별도)

검증용 FastAPI 프로세스는 종료했고 Redis 컨테이너는 실행 상태로 유지합니다.

## Engine Redis 소비자 (세그먼트 수신 단계)

서버 lifespan이 소비자 태스크 1개를 시작하고 종료 시 취소·정리합니다.
`python -m app`은 worker 1개입니다. 별도 uvicorn 실행 시에도 --workers 1을 사용하고
같은 방송을 소비하는 백엔드를 중복 실행하지 마세요.

- `ONAIR_STATION_ID`: 기본 st_local_dev
- `ONAIR_AUDIO_DIR`: 공유 오디오 루트의 절대 경로. 생략하면 저장소 apps/engine/var/audio
- `ONAIR_REDIS_URL`: 엔진과 동일한 변수 이름 지원. 설정하면 기존 REDIS_URL보다 우선
- 엔진은 apps/engine에서 실행하며 transport.audio_dir가 백엔드 루트와 같은 실제 폴더여야 함

실제 키는 **engine:st_local_dev:out**이며 중괄호는 포함하지 않습니다.
consumer group은 backend, 시작 위치는 0-0입니다. 기존 pending은 30초 유휴 후 회수합니다.

수신 흐름: v1 envelope/metadata 검증 → 방송별 경로/파일 존재 확인 →
Redis의 segment hash와 FIFO list에 원자적 중복 방지 저장 → XACK.
동일 ID의 다른 내용은 격리합니다. 파일 누락은 30초 이상 간격으로 총 3회 시도합니다.
영구 오류는 engine:st_local_dev:out:dead에 기록한 뒤 ACK합니다.
request.state는 별도 Redis hash에 보관하며 오래된 pending이 새 상태를 덮어쓰지 않습니다.

확인 API:

- GET /api/broadcast/queue: 저장된 세그먼트 개수와 앞 100개 (재생 가능한 HLS 버퍼를 뜻하지 않음)
- GET /api/requests/{request_id}/state: 엔진에서 받은 상태, 미수신이면 404
- Redis 장애 시 위 API는 503

파일은 WAV/MP3 확장자·존재·빈 파일 여부와 경로를 검사합니다. 실제 코덱/길이 검증은
FFmpeg로 디코딩하며, HLS 길이는 실제 인코딩 결과를 사용합니다. 원본 파일은 수정·삭제하지 않습니다.
request.state 수신은 구현했지만 POST /api/requests의 엔진 발행과 기존 요청 목록 상태 병합은 후속입니다.
중복 기록/큐는 자동 만료하지 않습니다. Redis 영속성은 Compose AOF/볼륨 설정에 의존합니다.

### 통합 테스트

저장소 루트에서 엔진을 테스트 환경에 설치합니다:
`apps/backend/.venv/Scripts/python.exe -m pip install -e "apps/engine[redis]"`

Redis가 실행된 상태에서 PowerShell:

```powershell
$env:ONAIR_INTEGRATION_TEST = "1"
apps/backend/.venv/Scripts/python.exe -m unittest discover -s apps/backend/tests -v
Remove-Item Env:ONAIR_INTEGRATION_TEST
```

통합 테스트는 localhost:6379의 UUID 테스트 방송 키만 사용하고 종료 시 해당 키만 지웁니다.
실제 엔진 파이프라인의 WAV 제출, 중복, ACK 전 장애 후 pending 회수, 경로 거부,
파일 누락 재시도, ID 충돌, 상태 순서 및 lifespan 소비 루프를 확인합니다.

검증 결과: 기존 회귀 및 실제 Redis/엔진 통합을 포함한 11개 테스트 통과.


## HLS 라이브 재생

FFmpeg를 먼저 설치하고 `ffmpeg -version`을 확인합니다. PATH에서 찾을 수 없으면
백엔드 .env의 `ONAIR_FFMPEG`에 ffmpeg.exe 절대 경로를 지정합니다.
`ONAIR_HLS_ENABLED=1`이 기본이며, Redis 수신만 검증하려면 0으로 설정합니다.
출력은 apps/backend/var/hls이며 `ONAIR_HLS_DIR`로 변경할 수 있습니다.

1. 저장소 루트: `docker compose up -d redis`
2. apps/backend: `.\.venv\Scripts\python.exe -m app`
3. apps/engine: 엔진 실행 환경에서 `python -m onair_engine --transport redis --tts dummy`
   (dummy는 무음입니다. 음성 확인에는 엔진의 실제 TTS 설정이 필요합니다.)
4. apps/frontend: .env.example을 참고해 `VITE_API_BASE_URL=http://127.0.0.1:3000` 설정 후 `npm run dev`
5. /listen 화면의 방송 재생 버튼을 누릅니다. 초기 버퍼 준비에 약 15초가 필요합니다.

- GET /api/stream/state: HLS 준비 상태와 hlsUrl, 발행기 오류
- GET /hls/st_local_dev/index.m3u8: 라이브 목록 (준비 전 503)
- GET /hls/st_local_dev/{파일명}.ts: AAC 오디오가 담긴 TS
- 세그먼트별 음량 정규화 후 약 4초 조각 생성, 최소 24초 목록 유지
- 소스 경계에 DISCONTINUITY 삽입, 목록은 임시 파일에서 원자적으로 교체
- 큐가 비면 무음으로 방송 유지. 실제 청취는 발행보다 버퍼만큼 늦음

### 현재 범위와 한계

오디오 재생은 연동되어 있지만, 사연 전송·채팅·청취자 수·요청 상태 UI에는 아직 mock이 남아 있습니다.
WebSocket은 미연동이므로 화면의 연결 상태는 HLS 재생 여부와 별개입니다.
POST /api/requests → 엔진 request.arrived 발행은 다음 단계입니다.
HLS에 마지막 조각을 발행한 뒤 큐에서 제거하며, 실제 청취 완료(PLAYED)를 의미하지 않습니다.
중간 재시작 시 큐 선두 음원이 반복될 수 있습니다. 같은 방송에 백엔드를 중복 실행하면 안 됩니다.
Redis 조회 장애 중에는 무음을 공급하지만, 큐 제거/오류 기록 중 장애가 나면 발행기가 중단되어
복구 후 백엔드 재시작이 필요할 수 있습니다. /api/stream/state의 error를 확인합니다.
현재 세션에서 퇴출된 TS는 60초 뒤 정리합니다. 이전 실행의 출력과 원본 음원,
Redis 중복 기록은 자동 만료하지 않아 장기 운영 전 보존 정책이 필요합니다.

### 검증

ONAIR_INTEGRATION_TEST=1과 ONAIR_FFMPEG를 설정하고 unittest를 실행하면
실제 엔진/Redis, WAV·MP3 인코딩, HLS 디코딩, 요청 전달·재시작·중복 방지를 포함한 17개 테스트를 실행합니다.
프론트엔드는 `npm.cmd run build`, `npm.cmd run lint`로 확인합니다.

#### 4주차 백엔드 통합 검증 (2026-09-29)

기존 테스트 14개와 추가한 `test_broadcast_e2e.py` 1개가 실제 로컬 Redis·FFmpeg 환경에서 모두 통과했습니다.
추가 테스트는 로컬 테스트 음원을 엔진의 RedisTransport로 전달한 뒤 별도 포트의 실제 백엔드 프로세스를 실행합니다.

- WAV → MP3 원본 세그먼트가 FIFO 순서로 공개되는지 확인합니다.
- 준비된 방송에 중간 접속하여 HTTP HLS를 FFmpeg로 디코딩하고, 출력 PCM이 무음이 아닌지 확인합니다.
- 각 TS의 HTTP 다운로드와 metadata 조회를 확인합니다.
- 원본 큐가 소진되면 무음이 공급되고 MEDIA-SEQUENCE가 증가하는지 확인합니다.
- Redis 대기 큐와 미확인 이벤트(pending)가 모두 비었는지 확인합니다.

백엔드 폴더에서 다음과 같이 단독 실행할 수 있습니다. Docker Redis가 실행 중이고 엔진 패키지가 설치되어 있어야 합니다.

```powershell
$env:ONAIR_INTEGRATION_TEST = "1"
$env:ONAIR_FFMPEG = (Get-Command ffmpeg).Source # PATH에 없으면 ffmpeg.exe 절대 경로 지정
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_broadcast_e2e.py -v
Remove-Item Env:ONAIR_INTEGRATION_TEST
```

약 30초가 소요되며 임시 백엔드와 UUID 테스트 방송 데이터는 종료 시 정리합니다. 기존 방송 데이터와 Redis 컨테이너는 유지합니다.
이 검증은 백엔드 송출 경로를 대상으로 하며 외부 LLM/TTS 호출, 브라우저 음질 청취, 장시간 무중단·장애 복구를 보장하지 않습니다.

## Windows에서 실제 음성 테스트 (처음부터 실행)

아래는 백엔드 3001, 프론트엔드 18080 포트를 사용하는 수동 청취 테스트입니다.
각 터미널은 저장소 루트에서 시작합니다. 서버가 이미 실행 중이면 중복 실행하지 않습니다.
Python 3.12 이상, Node.js, Docker Desktop, FFmpeg가 설치되어 있어야 합니다.

### 최초 1회: 패키지 준비

저장소 루트 PowerShell에서 실행합니다. 기존 .venv가 있으면 생성 명령은 생략합니다.

```powershell
python -m venv apps/backend/.venv
apps/backend/.venv/Scripts/python.exe -m pip install -e ./apps/backend -e "./apps/engine[redis,tts]"
Push-Location apps/frontend
npm.cmd ci
Pop-Location
```

FFmpeg는 `ffmpeg -version`으로 확인합니다. 찾을 수 없으면 아래 백엔드 실행 명령의
`$env:ONAIR_FFMPEG = "ffmpeg"` 값을 설치된 ffmpeg.exe의 절대 경로로 바꿉니다.

### 1. Docker와 Redis

Docker Desktop을 열고 엔진 시작이 완료된 뒤 저장소 루트에서 실행합니다.

```powershell
docker compose up -d redis
docker compose exec redis redis-cli ping
```

`PONG`이 나와야 합니다. Docker는 Redis를 실행하며 나머지 프로그램은 아래 터미널에서 실행합니다.

### 2. 터미널 1: 백엔드

```powershell
cd apps/backend
$env:PORT = "3001"
$env:ONAIR_REDIS_URL = "redis://127.0.0.1:6379/0"
$env:ONAIR_STATION_ID = "st_local_dev"
$env:ONAIR_HLS_ENABLED = "1"
$env:ONAIR_FFMPEG = "ffmpeg"
.\.venv\Scripts\python.exe -m app
```

이 터미널을 실행 상태로 둡니다. http://127.0.0.1:3001/health/redis 에서
`{"ok":true,"redis":"connected"}`를 확인합니다.

### 3. 터미널 2: 프론트엔드

새 PowerShell을 저장소 루트에서 열어 실행합니다.

```powershell
cd apps/frontend
$env:VITE_API_BASE_URL = "http://127.0.0.1:3001"
$env:VITE_WS_URL = ""
$env:VITE_HLS_URL = ""
npm.cmd run dev -- --host 127.0.0.1 --port 18080 --strictPort
```

http://127.0.0.1:18080/listen 을 열고 방송 상태가 준비됨으로 바뀌면 재생 버튼을 누릅니다.
초기 준비에는 약 15초가 걸립니다. 엔진 음원이 아직 없으면 기본 폴백 멜로디가 재생됩니다. ONAIR_FALLBACK_MODE=silence이면 무음입니다.

### 4. 터미널 3: 실제 음성 생성

새 PowerShell을 저장소 루트에서 열어 실행합니다.

```powershell
cd apps/engine
$env:ONAIR_REDIS_URL = "redis://127.0.0.1:6379/0"
..\backend\.venv\Scripts\python.exe -m onair_engine --transport redis --tts edge --max-segments 10
```

`edge`는 인터넷으로 실제 음성을 합성하고, `dummy`는 무음을 만듭니다.
현재 예시 설정의 대본 생성은 dummy이므로 정해진 예시 문장을 실제 목소리로 읽습니다.
생성이 끝나도 방송 버퍼만큼 늦게 들립니다. 모든 음원을 소비하면 폴백으로 돌아갑니다.
생성 수 제한 없이 실행하려면 `--max-segments 10`을 생략하고 종료할 때 Ctrl+C를 누릅니다.

### 자주 발생하는 오류

| 증상 | 조치 |
| --- | --- |
| npm.ps1 실행 정책 오류 | `npm` 대신 `npm.cmd` 사용. 실행 정책 변경은 불필요 |
| WinError 10048 / Port 18080 is already in use | 해당 포트에 서버가 이미 실행 중. 기존 화면/health를 확인하고 기존 서버를 사용하거나, 해당 서버 터미널에서 Ctrl+C 후 재실행 |
| edge TTS 의존성 오류 | 최초 패키지 설치 명령의 `engine[redis,tts]`를 같은 가상환경에 설치 |
| FFmpeg not found | ONAIR_FFMPEG에 설치된 ffmpeg.exe 절대 경로 지정 |
| 소리가 안 나옴 | 재생 버튼·PC 음소거·엔진 로그와 /api/stream/state의 status, error 확인 |
| HLS publisher stopped | 백엔드 오류 로그 확인 후 해당 백엔드만 Ctrl+C로 종료하고 재실행 |

방송 상태 URL: http://127.0.0.1:3001/api/stream/state
정상 준비 상태는 `status: ready`, `error: null`입니다. 준비됨 자체가 음성 존재를 뜻하지는 않습니다.
화면 위 WebSocket 연결 끊김 표시는 현재 HLS 재생 여부와 별개입니다.

### 재생 metadata API 확인 (백엔드)

- HLS 목록에 있는 TS 파일명으로 `GET /api/stream/state?fragment={TS 파일명}`을 호출합니다. 서버의 최신 인코딩 대상이 아닌 해당 조각의 원본 metadata를 반환합니다.
- 원본 음원은 `segment`, 빈 큐의 음악은 `fallback`, 무음 설정은 `silence`, 미등록·만료된 조각은 `unavailable` 상태인지 확인합니다.
- 화면 표시와 재생 위치에 따른 API 호출은 프론트엔드 담당자의 연동 작업으로 남겨 둡니다. 방송 클럭·실제 잔여 버퍼 계산은 백엔드에서 제공합니다.
- 세부 응답 및 오류는 [방송 처리 및 복구](../../docs/BACKEND_BROADCAST_RUNTIME.md)를 참고합니다. 선택 쿼리의 형식은 Swagger `/docs`에도 반영됩니다.
같은 방송의 백엔드를 다른 포트로 중복 실행하지 않습니다.

### 종료와 다음 실행

엔진 → 프론트엔드 → 백엔드 터미널 순서로 Ctrl+C를 누릅니다.
저장소 루트에서 `docker compose stop redis`로 Redis를 중지한 뒤 Docker Desktop을 종료합니다.
Redis에 남은 큐는 다음 실행 때 처리될 수 있습니다.
다음 테스트에서는 패키지 설치를 반복하지 않고 1~4번을 진행합니다.
`$env:...` 설정은 해당 터미널에만 적용되므로 새 터미널에서는 다시 입력합니다.


## 방송 상태·복구와 통합 테스트

설정, 버퍼 계산, 서버 시간 기준 PLAYED, 폴백과 재시작 정책은
[방송 처리 및 복구](../../docs/BACKEND_BROADCAST_RUNTIME.md)를 참고합니다.

저장소 루트 PowerShell에서 Docker Desktop을 실행한 후:

```powershell
docker compose up -d redis
./apps/backend/scripts/test.ps1 -Integration -FFmpeg "ffmpeg"
```

FFmpeg가 PATH에 없으면 `-FFmpeg`에 ffmpeg.exe 절대 경로를 넣습니다.
스크립트 실행 정책으로 차단되면 아래 명령을 사용합니다. 시스템 정책을 변경하지 않습니다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./apps/backend/scripts/test.ps1 -Integration -FFmpeg "ffmpeg"
```

최초 통합 테스트 전에는 `.\apps\backend\.venv\Scripts\python.exe -m pip install -e "apps/engine[redis]"`를
저장소 루트에서 실행해야 합니다. 실제 TTS 청취 테스트는 위의 `tts` 설치 절차를 따릅니다.
테스트는 고유 스테이션과 임시 서버·파일을 사용하며 해당 테스트 데이터만 정리합니다.
실행 중인 Redis는 종료하지 않습니다. 외부 LLM·TTS API는 호출하지 않습니다.
HTTP 스트림을 실제 FFmpeg로 디코딩하지만 스피커에서 들리는지는 사용자가 별도로 확인합니다.
