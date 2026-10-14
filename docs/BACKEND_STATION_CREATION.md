# 단일 방송 생성과 엔진 REST 연결

## 현재 범위

백엔드의 `ONAIR_STATION_ID`로 지정된 방송 **하나**를 API로 생성한다.
동시 방송은 하나이며, 종료 정리가 끝나면 같은 ID로 새 방송을 생성할 수 있다.
신청곡과 프론트엔드 생성 폼은 별도로 연동되어 있다. 동적 다중 방송은 지원하지 않는다.
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
| POST /api/stations/{station_id}/stop | 소유자만 종료·정리 요청, 본문 `{created_at: 조회한 생성 시각}`, 202 |

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

### 방송 형식 선택 (#102 백엔드)

같은 본문에 다음 선택 필드를 추가할 수 있다. FastAPI `/docs`에도 필드와 enum이 노출된다.

| 필드 | 허용 값 / 제한 |
| --- | --- |
| `format` | `music`, `talk`, `news`; 기본 `talk` |
| `corners` | talk만; story_corner/text_corner/topic_talk/dj_vs_listeners 중 중복 없이 2~3개 |
| `news_sections` | news만; general/economy/it/sports/culture 중 중복 없이 2~3개 |
| `intensity` | low/mid/high; 기본 mid, news에서는 엔진이 무시 |

`corners`·`news_sections`를 생략하면 엔진 기본 편성을 사용한다. `experiment`는 호스트 API에서
받지 않으며 전달하지 않는다(422). 프론트 화면은 별도 작업이다.

선택 필드가 하나도 없는 기존 요청은 기존 v1 페이로드와 재시도 지문을 유지한다.
선택 필드가 있는 요청은 `station.created`에 추가 필드를 전달한다. 이 요청으로 방송을 시작하려면
엔진 M3(PR #108)의 공용 스키마·엔진 변경이 먼저 배포되어야 한다. 현재 v1 엔진은 새 필드를
거부할 수 있으므로 백엔드 API 준비만으로 전체 형식 선택 기능이 완료되는 것은 아니다.

## 전달과 상태

1. 스키마 검증 → 최종 persona·방송 설정 저장 + `engine:control`에 `station.created` 발행.
2. 동일 사용자·키·본문 재시도는 기존 방송을 반환한다. 다른 요청은 진행 중 방송이 있으면 409. `ended`이면 새 키로 생성 가능하다.
3. 엔진 `--serve`가 요청을 받아 시작하면 `station.started`, 실패하면 `station.rejected`를 보낸다.
4. 백엔드가 해당 이벤트를 저장한 뒤 ACK한다. 이미 반영된 결과와 생성 시각 이전 이벤트는 상태를 되돌리지 않는다.

`station.status`는 `preparing / started / rejected / draining / stopping / cleaning / ended`다. `started`는 엔진 준비 완료이며
실제 청취 완료나 HLS 준비를 뜻하지 않는다. `hls_ready`는 별도 값이고 폴백으로도 true가 될 수 있다.
엔진이 생성 요청을 소비하지 않았으면 `preparing`에 머문다. 시작한 방송의 종료는 아래 절차로 자동 감지한다.
기존 `/api/stream/state` 및 `/api/requests`는 설정된 단일 방송을 계속 사용한다.

새 방송은 `ended` 확인 후 새로운 `Idempotency-Key`로 생성한다. 기존 키 재시도는 이전 방송을 반환한다.
기존 수동 로컬 엔진과 API로 생성한 엔진을 같은 station_id로 동시에 실행하지 않는다.

### 방송 종료와 정리

- 백엔드는 인증된 `GET /v1/engine/status`를 5초마다 확인한다. 생성 이벤트 처리 완료 후 해당 방송이
  정상 응답에서 3회 연속 사라지면 종료로 판단한다. 연결 실패·인증 실패·잘못된 응답은 종료로 판단하지 않는다.
- 자동 종료는 `draining`으로 전환하고, 이미 받은 송출 대상과 서버 방송 시간표의 재생 완료 및
  상태 이벤트 전달을 기다린다. 소개가 없는 신청곡 후보는 무한정 기다리지 않는다.
- 소유자가 청취 화면의 **방송 종료·정리**를 누르고 확인하면 `stopping`으로 전환한다.
  기존 `station.closed` 이벤트를 엔진에 보내며, 엔진이 사라진 것을 확인한 후 정리한다.
  수동 종료는 남은 음원을 끝까지 송출하지 않는다. 다른 사용자는 버튼이 없고 API도 404다.
- `cleaning` 동안 publisher를 중지하고 다시듣기를 확정한다. 현재 대기열·신청곡 목록·Redis 입출력 스트림은
  `archive:{generation}:...`에 보관한다. SQLite 송출 저널은 `ended-{generation}.sqlite`로 보관한다.
- 원본 음원, VOD, persona, 요청·세그먼트 이력은 보존한다. 미완료 요청은 `failed`와
  `broadcast_ended` 사유로 종결한다. 이전 다시듣기의 소유권은 새 방송 소유자에게 넘어가지 않는다.
- 정리 후 `ended`로 표시하고 폴백 송출을 중단한다. 다음 방송은 새 HLS 세션으로 시작한다.
  공개 `GET /api/stream/state`의 `stationStatus`로 종료 화면을 갱신한다.
- 종료 요청은 반복해도 안전하다. 본문의 `created_at`이 현재 방송과 다르면 409로 거부하여
  오래 열린 화면에서 새 방송을 종료하는 것을 막는다. Redis/파일 오류가 나면 정리 상태를 보존하고 재시도한다.
- 생성 요청이 아직 처리되지 않았거나 엔진 상태를 확인할 수 없으면 정리가 대기할 수 있다.
  이때 진행 중인 방송을 삭제해 강제로 새 방송을 만드는 동작은 하지 않는다.

정리 기능은 자료를 영구 삭제하는 저장 공간 청소 기능이 아니다. 아카이브 보존 기간과 디스크 회수는 별도 운영 정책이다.

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
