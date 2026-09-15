# onAIr Backend — FastAPI 전환 1단계

기존 Node API를 FastAPI로 이식했습니다. Redis와 HLS는 아직 연결하지 않았습니다.
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
로컬 테스트는 한 서버 프로세스로 시작합니다. 방송 소비자는 후속 단계에서 별도로 설계합니다.

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

큐는 프로세스 메모리에만 존재하며 재시작 시 사라집니다. 반드시 worker 1개로 실행합니다.
tick은 정책 판단만 수행하며 요청을 소비하거나 오디오를 생성하지 않습니다.
Engine stdout/mock frontend는 그대로이고 POST /api/segments는 없습니다.

## 회귀 검증

백엔드 폴더에서 `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`.
테스트는 별도 임시 포트의 실제 HTTP 서버를 사용하고 종료합니다.

전체 계획은 [전환 계획](../../docs/BACKEND_MIGRATION.md)을 참고하세요.
