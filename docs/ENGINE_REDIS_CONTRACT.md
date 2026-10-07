# 엔진 ↔ 백엔드 통신 계약 (초안 v1)

- 상태: **흐름(Redis)은 엔진 구현 완료, 하트비트·조회 REST는 미구현. 백엔드 담당 합의 전 초안.** 필드나 의미를 바꾸면 `contract_version`을 올린다.
- **결정 (2026-09-20, 확인 1)**
  - **흐름은 Redis Streams** — 세그먼트 제출, 요청 전달, 상태 통보, 스테이션 수명주기
  - **관측도 Redis 푸시가 기본** — 엔진이 `engine.health` 하트비트로 상태를 밀어주고, 백엔드가 그 값으로 운영자 콘솔(F-14)을 구성한다 (설계 문서 5.3의 원래 방향)
  - **조회 REST는 푸시로 감당이 안 되는 것만** — 5개 (6장). 엔진은 이 API 한정으로 포트를 하나 연다
  - **오디오는 공유 파일시스템 경로** — 페이로드에는 `audio_ref`만 싣는다
- 엔진 구현: `apps/engine/src/onair_engine/transport.py`의 `RedisTransport`
- 근거: 노션 "백엔드→엔진" API 표, [ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) 5·6장, [BACKEND_MIGRATION.md](BACKEND_MIGRATION.md) "Redis 전달 계약 제안"

## 1. 스트림

| 스트림 | 방향 | 생산자 | 소비 그룹 |
|---|---|---|---|
| `engine:{station_id}:out` | 엔진 → 백엔드 | 엔진 | `backend` (백엔드가 생성) |
| `engine:{station_id}:in` | 백엔드 → 엔진 | 백엔드 | `engine` (엔진이 구독 시작 시 `MKSTREAM`으로 생성) |
| `engine:control` | 백엔드 → 엔진 | 백엔드 | `engine` — `station.created` 전용. **미구현 (M3)** |

엔진은 `XADD ... MAXLEN ~ 10000`으로 발행한다. 백엔드도 `:in`에 같은 상한을 권장한다.

## 2. 메시지 envelope

스트림 엔트리의 필드는 모두 문자열이다.

| 필드 | 예 | 설명 |
|---|---|---|
| `type` | `segment.submitted` | 이벤트 종류 (3장) |
| `contract_version` | `1` | 이 문서의 버전 |
| `event_id` | `evt_3f2a9c1b04de` | 발행자가 발급하는 멱등 키 |
| `station_id` | `st_local_dev` | 스트림 이름과 같은 값 (검증용) |
| `at` | `1789496813.249` | 발행 시각, Unix 초 |
| `payload` | `{"id": "seg_..."}` | 이벤트별 본문, JSON 문자열 |

## 3. 이벤트

### 3.1 엔진 → 백엔드 (`:out`)

| type | payload | 상태 |
|---|---|---|
| `segment.submitted` | `SegmentSubmission` (설계 문서 5.1) | 구현 |
| `request.state` | `{"request_id", "state"}` — state: `screened` `queued` `generating` `generated` `rejected` | 구현 |
| `engine.health` | 3.1.1 참고 — 운영자 콘솔이 쓰는 스테이션 현재 상태 | **미구현 — 이번 결정으로 범위 확정** |

`segment.submitted` payload 예:

```json
{
  "id": "seg_9be9401b85d7",
  "station_id": "st_local_dev",
  "audio_ref": "st_local_dev/seg_9be9401b85d7.mp3",
  "duration_ms": 5496,
  "corner_type": "filler",
  "kind": "filler",
  "priority": 50,
  "reorderable": true,
  "request_ref": null,
  "music_ref": null,
  "created_at": 1789496813.26
}
```

### 3.1.1 `engine.health` 하트비트

스테이션마다 주기적으로(**제안: 5초**, 합의 항목) 발행한다. 운영자 콘솔이 필요로 하는 값은 이 하나로 충족시키고, 백엔드는 마지막 값만 들고 있으면 된다.

```json
{
  "buffer_sec": 33.9,
  "inflight_generations": 1,
  "pending_requests": 2,
  "recent_latency_p95_ms": 2310,
  "order_position": 3,
  "next_slot": "filler",
  "policy_name": "naive_fifo",
  "recent_decisions": {"generate": 12, "wait": 43}
}
```

- **작고 자주 바뀌는 값만 싣는다.** 러닝오더 슬롯 배열·정책 파라미터처럼 거의 고정인 값은 `station.created` 시점에 백엔드가 이미 알고 있으므로 매번 보내지 않고, 위치(`order_position`)와 이름만 보낸다.
- **결정 로그 원본은 싣지 않는다.** `recent_decisions`는 직전 주기의 action별 건수 요약이다. 원본이 필요한 순간에만 REST로 조회한다(6장).

> **왜 결정 로그를 푸시하지 않는가 (실측 근거)**
> 엔진은 대기 중에도 0.5초마다 정책 판단을 기록한다 — 실측 **초당 1.2~2.1건, 한 건 평균 298바이트**. 스테이션 하나가 1시간 방송하면 약 5,400건(약 2MB)으로, 세그먼트 제출(시간당 300~600건)보다 10배 이상 많다.
> `MAXLEN ~10000`인 스트림에 함께 실으면 보존 구간이 **약 하루치에서 약 2시간치로** 줄어, 백엔드가 잠시 멈췄다 복귀할 때 세그먼트가 먼저 잘려나갈 위험이 생긴다.
> 또한 결정 스냅샷(`ScheduleContext`)은 정책 실험을 하며 계속 바뀌는 값이다. 계약에 넣으면 필드 하나 바꿀 때마다 `contract_version`을 올려야 하므로, 엔진 SQLite(설계 문서 7장)를 원천으로 두고 조회 창구만 연다.

### 3.2 백엔드 → 엔진 (`:in`, `station.created`만 `engine:control`)

| type | payload | 엔진 동작 | 상태 |
|---|---|---|---|
| `request.arrived` | `{"request_id", "kind": "story"\|"mood", "body", "requester_ref"}` | L0 검사 후 요청 큐 적재. **request_id는 백엔드 발급값을 그대로 쓴다** — 이후 `request.state`가 같은 id로 나간다 | 구현 |
| `station.closed` | `{"reason": "normal"\|"operator_stop"}` | 진행 중 생성 취소, 계측 플러시 후 인스턴스 종료 | 구현 (`reason`은 아직 미사용) |
| `station.created` | 3.3절 제안안 — `{"broadcast_minutes", "persona": {...}, "policy"}` | EngineManager가 StationEngine 기동, ack 캐시 사전 렌더링 | M3 — **페이로드 합의 전** |
| `request.cancelled` | `{"request_id", "reason": "listener"\|"operator"}` | 대기 큐에서 제거, 생성 중이면 결과 폐기 | **미구현 — 신규 합의 항목** |
| `backpressure` | `{"d_total_ms", "threshold_ms", "severity"}` | 정책 ScheduleContext에 반영 → filler 우선 생성·요청 반영 보류 | 수신만, 무시 (M3) |
| `state.transition` | `{"segment_id", "state": "MIXING"\|"PUBLISHED"\|"PLAYED", "at"}` | 재배치 가능 집합에서 제거, 계측 기록, 방송 맥락 갱신 | 수신만, 무시 (M3) |

`station.created`의 러닝오더·정책 설정은 백엔드가 보낸 값이므로, 백엔드는 그 스테이션의 슬롯 배열과 정책 파라미터를 이미 알고 있다. 하트비트에는 현재 위치만 실린다.

상태 전이를 여러 건 보낼 때 **일괄 전용 이벤트는 두지 않는다.** 스트림에 엔트리를 연달아 `XADD`하면 된다.

### 3.3 `station.created` 페이로드 제안 (엔진 측 초안 — 백엔드 합의 전)

호스트가 방을 만들면 백엔드가 `engine:control`에 넣는 메시지다. 엔진은 이 메시지 하나로 그 방의 DJ를 만들고 방송을 시작한다. envelope은 2장과 같고, `station_id`는 새 방의 ID다. 9장 미결 사항 2번에 대한 제안안이며, 수치는 모두 `[예시]`다.

```json
{
  "broadcast_minutes": 60,
  "persona": {
    "persona_id": "saebyeok",
    "dj_name": "새벽",
    "concept": "심야 스터디 라디오",
    "tone": "차분하고 따뜻한 존댓말. 말수가 적고 문장이 짧다",
    "examples": [
      "새벽 한 시가 넘었네요. 아직 책상 앞에 계신 분들, 어깨 한 번 내려놓고 갈게요.",
      "오늘 분량을 다 못 끝내도 괜찮아요. 여기까지 온 것도 꽤 멀리 온 거예요.",
      "졸리면 물 한 잔이요. 저도 지금 한 잔 따라 놨어요."
    ],
    "forbidden": ["큰 소리로 감탄하거나 텐션을 올리기", "공부를 더 하라고 다그치기"],
    "signature_phrases": ["천천히 가요"],
    "voice": "ko-KR-Chirp3-HD-Aoede"
  },
  "policy": "naive_fifo"
}
```

로컬에서 백엔드 대신 넣어 보기 (엔진 구독은 M3 구현 후):

```powershell
wsl redis-cli XADD engine:control '*' type station.created contract_version 1 event_id evt_demo2 station_id st_8f2a at 1789500000 payload '{"broadcast_minutes":60,"persona":{"dj_name":"새벽","concept":"심야 스터디 라디오","tone":"차분하고 따뜻한 존댓말"}}'
```

#### 필드

| 필드 | 타입 | 필수 | 없을 때 | 제한 `[예시]` | 엔진이 쓰는 곳 |
|---|---|---|---|---|---|
| `broadcast_minutes` | 정수 | O | — | 10~720 | 방송 종료 시각 |
| `persona.persona_id` | 문자열 | | `null` | 64자 | 어떤 템플릿에서 만든 DJ인지 계측에 기록만 한다 |
| `persona.dj_name` | 문자열 | O | — | 1~20자 | 시스템 프롬프트 정체성 |
| `persona.concept` | 문자열 | O | — | 1~60자 | 시스템 프롬프트 정체성 |
| `persona.tone` | 문자열 | O | — | 1~100자 | 시스템 프롬프트 말투 |
| `persona.examples` | 문자열 배열 | | `[]` | 0~5개, 각 150자 (3개 이상 권장) | 말투 예시 — 말투를 가장 강하게 고정한다 |
| `persona.forbidden` | 문자열 배열 | | `[]` | 0~10개, 각 50자 | 시스템 프롬프트 금지 사항 |
| `persona.signature_phrases` | 문자열 배열 | | `[]` | 0~3개, 각 30자 | 입버릇 (가끔만 쓰도록 지시) |
| `persona.voice` | 문자열 | | 엔진 기본 보이스 | 엔진 TTS 제공자의 보이스 ID | TTS 어댑터, ack 사전 렌더링 |
| `policy` | 문자열 | | `naive_fifo` | 현재 `naive_fifo`만 | 편성 정책 선택 |

- `persona`의 6개 말투 필드는 엔진에 구현돼 있다 (`StationProfile`, `config/personas/*.yaml`과 같은 이름). `voice`는 아직 엔진 전체 설정이라 방별 적용은 M3에서 구현한다 ([persona.md](persona.md) 9.3절).
- `running_order`는 v1에서 넣지 않는다. 지금 편성은 엔진 고정값이고, 정책 A/B/C가 확정되면(기획서 8장) 그때 필드를 추가하고 `contract_version`을 올린다.
- 행동 층(`behavior` — 요청 반영 시점·빈도, [persona.md](persona.md) 2절)도 같은 이유로 v1에서 뺀다.

#### 엔진 측 제안 (결정이 필요한 5가지)

1. **persona는 ID가 아니라 본문을 싣는다.** 엔진이 persona 저장소를 몰라도 되고, 받은 페이로드를 그대로 계측 DB에 남기면 그것이 방송 시점의 사본(`persona_snapshot`, [persona.md](persona.md) 9.4절)이 된다. `persona_id`는 출처 기록용으로 함께 보낸다.
2. **필드 이름은 엔진 코드 기준(`dj_name` 등)으로 맞춘다.** 엔진 구현·테스트가 이 이름을 쓰고, [persona.md](persona.md) 2절의 `name`은 예시 표기였다. 백엔드에는 아직 방 모델이 없다.
3. **`broadcast_minutes`는 persona 밖에 둔다.** 기존 초안은 `profile` 안에 있었지만, 방송 시간은 DJ가 아니라 방의 속성이다. 같은 persona를 여러 방이 다른 시간으로 쓸 수 있다.
4. **목록은 JSON 배열로 보낸다.** 문자열 하나로 보내면 거부한다 (한 글자씩 예시로 쪼개지는 사고 방지). 시각 형식은 envelope `at` 규칙(9장 미결 1)을 따른다.
5. **엔진은 시작 결과를 알린다 (신규 이벤트 제안).** 둘 다 `engine:{station_id}:out`으로 나간다.

   | type | payload | 의미 |
   |---|---|---|
   | `station.started` | `{}` | 검증 통과, ack 사전 렌더링 완료 — 첫 세그먼트가 곧 나온다 |
   | `station.rejected` | `{"reason": "invalid_payload"\|"unknown_voice"\|"llm_unavailable", "detail"}` | 방송을 시작하지 않았다. `detail`은 사람이 읽는 원인 (예: `persona.examples: 6개 — 최대 5개`) |

#### 백엔드에 확인할 것

- 방 생성 화면에서 위 필드를 **누가 채우는가** — 호스트가 필드별로 입력, 프리셋 선택, 자유 문장을 LLM이 변환([persona.md](persona.md) 3절) 중 무엇인가. 프리셋을 쓴다면 엔진의 3종(`saebyeok`, `haessal`, `dodo`)을 기본 템플릿으로 노출할 수 있다.
- **호스트 입력의 내용 검사** — `examples`·`forbidden`은 호스트가 쓴 글이고 시스템 프롬프트에 들어간다. 인젝션은 데이터 블록으로 대비돼 있지만, 욕설·개인정보 검사는 지금 아무 데서도 하지 않는다. 방 생성 시 백엔드가 할지, 엔진이 받을 때 L0로 할지 정한다.
- 위 길이 제한 값과 필수 여부.
- `voice` 목록을 프론트에 어떻게 보여줄지 — 엔진 TTS 제공자(확인 3)가 정해져야 목록이 확정된다.

## 4. 전달 보장

- **at-least-once.** 엔진은 이벤트 처리가 끝난 뒤 `XACK`한다. 엔진이 처리 도중 죽으면 재기동 시 자기 consumer의 pending(ID `0`)부터 다시 처리한다. 백엔드는 `event_id`로 중복을 무시할 수 있어야 한다.
- 엔진 consumer 이름은 `engine-{hostname}`이다. 재기동해도 같은 이름이어야 pending을 되찾는다.
- 계약 위반 메시지(필드 누락, JSON 오류)는 엔진이 `TRANSPORT_BAD_EVENT` 로그를 남기고 ACK한다. 붙잡고 있으면 뒤 메시지가 전부 막히기 때문이다.
- 엔진 발행이 실패하면(Redis 다운) `TRANSPORT_FAILED` 로그만 남기고 방송을 계속한다. 재제출 큐는 M2.
- 엔진의 이벤트 수신 루프는 연결이 끊기면 3초 뒤 다시 구독한다.
- `engine.health`는 **최신값만 의미 있는 이벤트**다. 백엔드는 밀린 하트비트를 따라잡을 필요 없이 마지막 것만 반영하고 ACK하면 된다.

백엔드 소비 측 권장 사항:

- `XREADGROUP GROUP backend <consumer>` → 검증 → 방송 큐 적재 → `XACK` 순서. 적재 전에 ACK하지 않는다.
- 세그먼트 중복 방지는 `payload.id` 기준 (예: `SET seen:{id} 1 NX EX 86400`).
- 죽은 consumer의 pending은 `XAUTOCLAIM`(min-idle 예: 30초)으로 회수한다.
- **방송별 소비자는 하나**여야 한다. HTTP worker마다 consumer를 띄우면 세그먼트가 여러 FFmpeg 파이프라인으로 흩어진다.
- Redis 재기동 시 스트림이 유실되지 않도록 AOF(`appendonly yes`, `appendfsync everysec`)를 켠다.

## 5. 오디오 파일

- **공유 오디오 루트**: 엔진 `transport.audio_dir`과 백엔드 설정이 같은 디렉토리를 가리킨다. 단일 머신 배포 전제다.
- **audio_ref**: 루트 기준 POSIX 상대 경로 — `{station_id}/seg_xxx.mp3`, `{station_id}/ack/ack_N.mp3`. 백엔드는 루트 밖으로 벗어나는 경로(`..`, 절대 경로)를 거부하고 파일 존재를 확인한다.
- **곡 음원**: 같은 루트의 `music/` 아래에 두고 `music_ref`(= `track_id`)로 지정한다. 엔진은 읽기만 한다. 파일명 규칙은 실음원 도입(M1) 때 확정한다 — **합의 항목**
- **원자적 쓰기**: 엔진은 같은 디렉토리에 `.{이름}.{랜덤}.tmp{확장자}`로 쓴 뒤 `os.replace`로 교체하고, 교체가 끝난 뒤에만 메시지를 발행한다. 메시지를 받았다면 파일은 완성본이다. 백엔드는 `.`으로 시작하는 파일을 무시한다.
- **포맷**: 제공자 원본 그대로 쓴다(google/edge는 mp3, dummy는 wav). 확장자로 구분한다. 샘플레이트와 음량(loudnorm)은 백엔드 FFmpeg가 HLS로 변환할 때 정규화한다.
- **duration_ms**: 엔진이 파일에서 실측한다(mutagen).
- **읽기 전용**: 오디오 파일은 엔진 TTS 캐시와 하드링크로 내용을 공유할 수 있으므로 제자리에서 수정하지 않는다. 삭제는 괜찮다.
- **TTS 캐시**: 엔진 전용이며 공유 루트 밖(`var/tts_cache`)에 있다. 백엔드와 무관하다.

### 5.1 정리 정책 (합의안 — 미구현)

| 대상 | 삭제 주체 | 시점 |
|---|---|---|
| `{station_id}/seg_*` (제출된 세그먼트) | 백엔드 | `PLAYED` 후 보존 기간(기본 24시간) 경과 |
| `{station_id}/ack/*` | 백엔드 | 여러 번 재사용되므로 `PLAYED`로 지우지 않는다. 방송 종료 후 보존 기간이 지나면 디렉토리째 삭제 |
| 제출되지 않은 파일 (생성 취소 등) | 엔진 | 기동 시·주기적으로, 발행 기록에 없고 1시간 이상 된 파일 |
| `.`으로 시작하는 임시 파일 | 엔진 | 비정상 종료 잔여물 — 1시간 이상 된 것 |
| TTS 캐시 | 엔진 | 용량 상한 초과 시 오래 안 쓴 것부터 (LRU) |

## 6. 조회 REST (엔진이 여는 API) — 미구현

**GET만, 5개만 둔다.** 상태를 바꾸는 호출은 전부 Redis 이벤트로 가고, 스테이션 현재 상태는 하트비트로 간다.
여기 남은 것은 **푸시로 감당이 안 되는 두 종류**다 — ① 양이 많아 스트림에 실으면 안 되는 것(결정 로그), ② 거의 변하지 않아 푸시할 일이 없는 정적 데이터(곡 카탈로그).

| endpoint | 용도 | 남기는 이유 | 엔진 현황 |
|---|---|---|---|
| `GET /health` | 프로세스 생존 | 하트비트가 끊긴 것이 "엔진 다운"인지 "Redis 문제"인지 가름 | 신규 (간단) |
| `GET /v1/engine/status` | 전역 상태 — 기동 스테이션 수, 동시 생성 여유, 지연 P95 | 하트비트는 스테이션 단위라 전역 합계가 없음 | **부분** — 스테이션 수·진행 중 생성 수는 있음. P95 집계와 **전역** 동시 생성 상한은 없음 |
| `GET /v1/stations/{stationId}/decisions` | 결정 로그 (생략 결정 포함) | 초당 1.5건 규모라 스트림에 실을 수 없음. 정책 비교 실험의 핵심 데이터 | 있음 (`decision_log`) — 페이지네이션 필요 |
| `GET /v1/catalog/tracks` | 곡 목록·검색 | 정적 데이터, 푸시 대상이 아님 | 있음 (더미 3곡) |
| `GET /v1/catalog/tracks/{trackId}` | 곡 제목·아티스트·길이·무드 | 백엔드가 `music_ref`로 곡 정보를 표시해야 함 | **부분** — 라이선스 필드 없음 |

### 6.1 REST로 두지 않고 푸시로 처리하는 것

| 노션 표 항목 | 대신 어떻게 | 비고 |
|---|---|---|
| 스테이션 목록·상태 조회 | `station.created`/`station.closed`로 목록을 알고, 상태는 `engine.health` | 백엔드가 자기가 만든 스테이션을 이미 알고 있다 |
| 요청 큐 조회, 요청 상태 조회 | `request.state` 전이를 쌓아 백엔드가 재구성. 대기 건수는 하트비트 `pending_requests` | 상태 머신의 원천은 어차피 엔진 통보다 |
| 러닝오더 조회 | 슬롯 배열은 `station.created`에 담긴 값, 현재 위치는 하트비트 `order_position` | 배열은 백엔드가 보낸 값이므로 되돌려줄 필요 없음 |
| 편성 정책 조회 | 파라미터는 `station.created`, 적용된 이름은 하트비트 `policy_name` | |
| 세그먼트 메타 재조회 | 백엔드가 `segment.submitted`를 자기 큐에 보관 | 엔진은 제출 후 메타를 보관하지 않는다 |

## 7. 노션 "백엔드→엔진" 표 매핑

| 노션 항목 | 결정 |
|---|---|
| 프로세스 헬스 체크 | REST `GET /health` |
| 엔진 전역 상태 조회 | REST `GET /v1/engine/status` |
| 스테이션 기동 | Redis `station.created` (`engine:control`) |
| 스테이션 목록·상태 조회 | **REST 없음** — `station.created`/`closed` + `engine.health` |
| 스테이션 종료 | Redis `station.closed` (`reason`) |
| 요청 전달 | Redis `request.arrived` |
| 요청 큐·상태 조회 | **REST 없음** — `request.state` 전이 + 하트비트 `pending_requests` |
| 요청 취소 | Redis `request.cancelled` — 엔진 미구현 |
| 백프레셔 통보 | Redis `backpressure` |
| 세그먼트 상태 전이 통보 | Redis `state.transition` |
| 상태 전이 일괄 통보 | **불필요** — 스트림에 연달아 XADD |
| 다음 세그먼트 가져오기 | **불필요** — 엔진이 `:out`으로 밀어준다 |
| 세그먼트 메타 재조회 | **REST 없음** — 백엔드가 `segment.submitted`를 보관 |
| 세그먼트 오디오 다운로드 | **불필요** — 공유 폴더 경로 |
| 곡 검색/목록, 곡 정보 조회 | REST (정적 데이터) |
| 곡 오디오 다운로드 | **불필요** — 공유 폴더 `music/` |
| 러닝오더 조회 | **REST 없음** — `station.created` + 하트비트 `order_position` |
| 편성 정책 조회 | **REST 없음** — `station.created` + 하트비트 `policy_name` |
| 결정 로그 조회 | REST `GET /v1/stations/{stationId}/decisions` |

**합계: REST 5개, Redis 이벤트 9개(엔진→백엔드 3개 포함), 불필요 4개, 푸시로 대체 5개.**

### 7.1 이름·표기 맞추기 (합의 필요)

| 항목 | 노션 표 | 엔진·이 문서 | 제안 |
|---|---|---|---|
| 요청 본문 | `text` | `body` | 이 문서 기준(`body`)으로 통일 — 엔진·계약이 이미 구현됨 |
| 요청자 식별 | `listener_ref` | `requester_ref` | 이 문서 기준(`requester_ref`)으로 통일 |
| 수신 시각 | payload `received_at` | envelope `at` | envelope `at`을 쓰고 payload에는 넣지 않는다 |
| 요청 상태 표기 | `QUEUED`, `GENERATING` (대문자) | `queued`, `generating` (소문자) | 전송은 소문자 고정, 콘솔 표시에서만 대문자 변환 |
| 백프레셔 단위 | `d_total_ms` | (미사용) | `_ms`로 통일 |
| 경로·필드 표기 | 경로 `{stationId}` camelCase | JSON `station_id` snake_case | 경로는 camelCase, JSON 필드는 snake_case 유지 |

## 8. 로컬에서 확인하기

```powershell
# Redis (WSL)
wsl sudo apt install -y redis-server
wsl redis-server --daemonize yes

# 엔진 (apps/engine)
onair-engine --transport redis --tts dummy

# 엔진이 발행한 메시지 보기
wsl redis-cli XRANGE engine:st_local_dev:out - + COUNT 5

# 백엔드 대신 청취자 요청 넣기
wsl redis-cli XADD engine:st_local_dev:in '*' type request.arrived contract_version 1 event_id evt_demo1 station_id st_local_dev at 1789500000 payload '{"request_id":"req_demo1","kind":"story","body":"요즘 잠이 안 와요","requester_ref":"listener_1"}'
```

## 9. 미결 사항 (백엔드와 합의 필요)

1. `at`·`created_at` 형식 — 현재 Unix 초(float), 설계 문서 5.1 예시는 ISO 8601
2. `engine:control`의 `station.created` 페이로드 — **엔진 측 제안안: 3.3절** (persona 본문, 필드·제한, 시작 결과 이벤트 `station.started`/`station.rejected`)
3. 백엔드 소비 그룹 이름과 생성 주체
4. Redis 인증 — 비밀번호/ACL은 `ONAIR_REDIS_URL` 환경변수(`redis://:pw@host:6379/0`)로 주입
5. `engine.health` 주기(제안 5초)와 필드 — 운영자 콘솔 화면에 실제로 필요한 값 확인
6. 5.1 정리 정책의 보존 기간
7. 7.1의 이름·표기 통일안
8. 조회 REST의 포트·인증(내부망 토큰?)·버전 접두사(`/v1`)
9. `request.cancelled` 형식과 "생성 중 취소"의 의미 (진행 중 결과를 어디까지 버릴지)
10. 곡 음원의 공유 폴더 경로 규칙과 카탈로그 라이선스 필드
11. 6.1의 "푸시로 대체" 항목 중 백엔드가 재구성하기 번거로운 것이 있는지 — 있으면 해당 항목만 REST로 되돌린다
