# 단일 방송 생성과 엔진 REST 연결

## 현재 범위

백엔드의 `ONAIR_STATION_ID`로 지정된 방송 **하나**를 API로 생성한다.
동적 다중 방송, 종료·재생성, 곡 재생, 프론트엔드 폼은 이번 단계에 포함하지 않는다.
persona 초안은 엔진에서 받고, 사용자가 선택·수정한 최종 persona는 방송 생성 시
백엔드가 `persona_id`를 발급해 Redis에 저장한다. 기존 persona 재사용 API는 아직 없다.

## API (모두 로그인 Bearer JWT 필요)

| API | 응답 / 용도 |
| --- | --- |
| GET /api/voices?native_only=true | 엔진의 한국어 보이스 목록 |
| POST /api/personas/drafts | `{form: PersonaForm, count: 1..5}` → 엔진의 `{drafts: [...]}` |
| POST /api/personas/check | `{persona: Persona}` → 엔진의 `{ok: true}` 또는 422 |
| POST /api/stations | `Idempotency-Key` 필수, 202 `{station: ...}` |
| GET /api/stations/{station_id} | 생성한 사용자만 조회, 없거나 다른 사용자 소유면 404 |

필드 제약은 `/docs`의 공용 스키마를 참고한다. 검증 오류는 FastAPI `detail` 안에 반환한다.
엔진 호출은 전체 30초로 제한한다. 엔진의 필드 오류는 `errors` 배열로,
FastAPI 형식 오류는 `detail` 배열로 원래 형태를 유지한다.
연결 실패 또는 엔진의 일시적 서비스 불가는 503, 시간 초과 504,
그 외 엔진 오류는 내부 인증 정보가 노출되지 않도록 일반 메시지의 502로 바꾼다.
엔진 API 토큰은 서버에만 두고 브라우저에는 사용자 JWT만 보낸다.

방송 생성 본문 예시:

```json
{
  "broadcast_minutes": 30,
  "topic": "함께 공부하는 밤",
  "policy": "naive_fifo",
  "persona": {
    "style": {"formality": "polite", "energy": "low", "humor": "rare"},
    "voice": "dummy-feminine",
    "dj_name": "새벽",
    "concept": "스터디 방송",
    "tone": "차분한 존댓말",
    "examples": ["안녕하세요.", "반가워요.", "쉬어 가요."]
  }
}
```

`voice` 예시는 dummy TTS용이다. 실제 TTS에서는 해당 제공자의 유효한 ID를 사용한다.
`persona_id`는 전송하지 않는다. `first_song`은 아직 재생 경로가 없어 422로 거부한다.

## 전달과 상태

1. 스키마 검증 → 최종 persona·방송 설정 저장 + `engine:control`에 `station.created` 발행.
2. 동일 사용자·키·본문 재시도는 기존 방송을 반환한다. 다른 요청은 409. 이벤트는 중복 발행하지 않는다.
3. 엔진 `--serve`가 요청을 받아 시작하면 `station.started`, 실패하면 `station.rejected`를 보낸다.
4. 백엔드가 해당 이벤트를 저장한 뒤 ACK한다. 이미 반영된 결과와 생성 시각 이전 이벤트는 상태를 되돌리지 않는다.

`station.status`는 `preparing / started / rejected`다. `started`는 엔진 준비 완료이며
실제 청취 완료나 HLS 준비를 뜻하지 않는다. `hls_ready`는 별도 값이고 폴백으로도 true가 될 수 있다.
엔진이 꺼져 있으면 `preparing`에 머문다. 이번 단계는 시작 결과만 관리하므로,
이후 엔진 종료·장애 상태를 자동 갱신하지 않는다. 재시작 자동 재기동도 보장하지 않는다.
기존 `/api/stream/state` 및 `/api/requests`는 설정된 단일 방송을 계속 사용한다.

현재는 같은 station_id를 새 방송으로 재사용하지 않는다. 새 테스트는 백엔드 종료 후
새 `ONAIR_STATION_ID`로 실행한다. 기존 자료를 임의 삭제해서 재사용하지 않는다.
기존 수동 로컬 엔진과 API로 생성한 엔진을 같은 station_id로 동시에 실행하지 않는다.

## 실행

저장소 루트에서 공용 패키지를 함께 설치한다:

```powershell
./apps/backend/.venv/Scripts/python.exe -m pip install -e packages/onair_schema -e apps/backend -e "apps/engine[redis,api]"
docker compose up -d redis
```

엔진 터미널 (외부 API 비용 없이 연동 확인):

```powershell
cd apps/engine
$env:ONAIR_REDIS_URL = "redis://127.0.0.1:6379/0"
../backend/.venv/Scripts/python.exe -m onair_engine --serve --transport redis --llm dummy --tts dummy
```

백엔드는 `ONAIR_ENGINE_API_URL=http://127.0.0.1:8100`으로 연결한다.
`ONAIR_ENGINE_TOKEN`을 설정한다면 엔진과 백엔드에 같은 값을 넣는다.
기존 README의 백엔드 실행 방식으로 시작하고, 로그인 후 위 API를 호출한다.
dummy TTS는 무음이며 실제 청취 검증과 구분한다. HLS를 켜려면 기존처럼 FFmpeg가 필요하다.

## 검증 범위

`test_stations.py`는 인증, 입력 검증, 엔진 오류 변환, 중복 요청, Redis 키 오류 시 부분 저장 방지,
실제 EngineManager(dummy LLM/TTS)의 시작 통보와 백엔드 결과 저장을 검증한다.
Redis 검증은 논리 DB 15의 고유 스테이션을 사용하며 자신이 만든 레코드만 정리한다.
외부 LLM·TTS 서비스는 호출하지 않는다.
