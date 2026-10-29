# 엔진 ↔ 백엔드 통신 계약 (v1)

- **v2 개정안 (2026-10-12, 백엔드 합의 전)**: 코너·입력 반영 재설계([CORNER_DESIGN.md](CORNER_DESIGN.md))에 따른 변경 제안은 **10장**에 있다. 1~9장은 현재 구현된 v1이다.
- 상태: **흐름(Redis) 중 세그먼트 제출·요청 전달·상태 통보는 엔진 구현 완료.** `station.created` 처리(3.3절)도 구현했다(#55). 엔진 REST(6장)도 구현했다(#64). 하트비트, 곡·사연 이벤트(3.4~3.5)는 미구현. 2026-10-08 팀 결정으로 9장의 미결 사항을 확정했다. 양쪽 모두 구현 전인 항목은 v1 안에서 확정하고, 이미 구현된 필드나 의미를 바꿀 때는 `contract_version`을 올린다.
- **결정 (2026-09-20, 확인 1)**
  - **흐름은 Redis Streams** — 세그먼트 제출, 요청 전달, 상태 통보, 스테이션 수명주기
  - **관측도 Redis 푸시가 기본** — 엔진이 `engine.health` 하트비트로 상태를 밀어주고, 백엔드가 그 값으로 운영자 콘솔(F-14)을 구성한다 (설계 문서 5.3의 원래 방향)
  - **조회 REST는 푸시로 감당이 안 되는 것만** (6장). 엔진은 이 API 한정으로 포트를 하나 연다
  - **오디오는 공유 파일시스템 경로** — 페이로드에는 `audio_ref`만 싣는다
- **결정 (2026-10-08, 팀 회의)**
  - `station.created`는 persona **본문을 통째로** 싣는다 (3.3절)
  - persona 저장소는 **백엔드 신규 DB** — 엔진은 저장하지 않고, 받은 페이로드를 계측 기록에 사본으로 남긴다
  - 호스트 입력 폼 → persona 변환은 **엔진**이 LLM으로 한다 (6.2절 REST)
  - 송출 기준 시점은 **`PUBLISHED`** ([context-memory.md](context-memory.md) 3.1절)
  - 음원은 카탈로그가 아니라 **YouTube 재생** (기획서 F-34) — 곡 정보는 백엔드가 엔진에 알려준다 (3.4절)
  - 9장 미결 사항 11개는 엔진 측 제안대로 확정
- 엔진 구현: `apps/engine/src/onair_engine/transport.py`의 `RedisTransport`
- 근거: 노션 "백엔드→엔진" API 표, [ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) 5·6장, [BACKEND_MIGRATION.md](BACKEND_MIGRATION.md) "Redis 전달 계약 제안", [persona.md](persona.md)

## 1. 스트림

| 스트림 | 방향 | 생산자 | 소비 그룹 |
|---|---|---|---|
| `engine:{station_id}:out` | 엔진 → 백엔드 | 엔진 | `backend` (백엔드가 기동 시 `MKSTREAM`으로 생성 — 구현됨) |
| `engine:{station_id}:in` | 백엔드 → 엔진 | 백엔드 | `engine` (엔진이 구독 시작 시 `MKSTREAM`으로 생성) |
| `engine:control` | 백엔드 → 엔진 | 백엔드 | `engine` — `station.created` 전용. 엔진이 구독 시작 시 `MKSTREAM`으로 생성 (`onair-engine --serve`) — 구현됨 |

엔진은 `XADD ... MAXLEN ~ 10000`으로 발행한다. 백엔드도 `:in`에 같은 상한을 권장한다.

## 2. 메시지 envelope

스트림 엔트리의 필드는 모두 문자열이다.

| 필드 | 예 | 설명 |
|---|---|---|
| `type` | `segment.submitted` | 이벤트 종류 (3장) |
| `contract_version` | `1` | 이 문서의 버전 |
| `event_id` | `evt_3f2a9c1b04de` | 발행자가 발급하는 멱등 키 |
| `station_id` | `st_local_dev` | 스트림 이름과 같은 값 (검증용) |
| `at` | `1789496813.249` | 발행 시각, **Unix 초(소수점 포함)** — 페이로드 안의 시각 필드도 같은 형식 (결정 2026-10-08) |
| `payload` | `{"id": "seg_..."}` | 이벤트별 본문, JSON 문자열 |

## 3. 이벤트

### 3.1 엔진 → 백엔드 (`:out`)

| type | payload | 상태 |
|---|---|---|
| `segment.submitted` | `SegmentSubmission` (설계 문서 5.1) | 구현 |
| `request.state` | `{"request_id", "state"}` — state: `screened` `queued` `generating` `generated` `rejected` `cancelled` | 구현 (`cancelled`는 미구현 — 3.2 `request.cancelled`) |
| `engine.health` | 3.1.1 참고 — 운영자 콘솔이 쓰는 스테이션 현재 상태 | 미구현 |
| `station.started` | `{"topic"}` — 검증 통과, ack 사전 렌더링 완료. 첫 세그먼트가 곧 나온다. `topic`은 이 방송의 확정된 주제(호스트가 비웠으면 엔진이 생성한 값) — 백엔드는 이 주제로 게시판 사연 후보를 고른다 (3.5절) | 구현 (호스트가 비운 주제는 아직 컨셉을 그대로 쓴다 — LLM 생성은 오프닝 코너 작업에서) |
| `station.rejected` | `{"reason": "invalid_payload"\|"unknown_voice"\|"llm_unavailable"\|"tts_unavailable", "detail"}` — 방송을 시작하지 않았다. `detail`은 사람이 읽는 원인 (예: `persona.examples: List should have at most 5 items after validation, not 6`). `tts_unavailable`은 TTS 키 누락·보이스 목록 조회 실패·ack 사전 렌더링 실패 (#55에서 추가 — 양쪽 구현 전이라 v1 안에서 확정) | 구현 |

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

- `music_ref`: 이 멘트 **바로 다음에** 틀 곡의 `track_ref` (3.4절). 곡 소개가 아니면 `null`.
- `request_ref`: 이 멘트가 반영한 요청의 `request_id` 또는 게시판 사연의 `story_id` (3.5절). 해당 없으면 `null`.

### 3.1.1 `engine.health` 하트비트

스테이션마다 **5초마다** 발행한다 (결정 2026-10-08). 운영자 콘솔이 필요로 하는 값은 이 하나로 충족시키고, 백엔드는 마지막 값만 들고 있으면 된다.

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
- 운영자 콘솔 화면이 확정되면 필드를 더할 수 있다. 필드 **추가**는 `contract_version`을 올리지 않는다 — 백엔드는 모르는 필드를 무시한다.

> **왜 결정 로그를 푸시하지 않는가 (실측 근거)**
> 엔진은 대기 중에도 0.5초마다 정책 판단을 기록한다 — 실측 **초당 1.2~2.1건, 한 건 평균 298바이트**. 스테이션 하나가 1시간 방송하면 약 5,400건(약 2MB)으로, 세그먼트 제출(시간당 300~600건)보다 10배 이상 많다.
> `MAXLEN ~10000`인 스트림에 함께 실으면 보존 구간이 **약 하루치에서 약 2시간치로** 줄어, 백엔드가 잠시 멈췄다 복귀할 때 세그먼트가 먼저 잘려나갈 위험이 생긴다.
> 또한 결정 스냅샷(`ScheduleContext`)은 정책 실험을 하며 계속 바뀌는 값이다. 계약에 넣으면 필드 하나 바꿀 때마다 `contract_version`을 올려야 하므로, 엔진 SQLite(설계 문서 7장)를 원천으로 두고 조회 창구만 연다.

### 3.2 백엔드 → 엔진 (`:in`, `station.created`만 `engine:control`)

| type | payload | 엔진 동작 | 상태 |
|---|---|---|---|
| `request.arrived` | `{"request_id", "kind": "story"\|"mood"\|"song", "body", "requester_ref", "track"?}` — `track`은 `kind: "song"`일 때만 (3.4절) | L0 검사(`body`만) 후 요청 큐 적재. **request_id는 백엔드 발급값을 그대로 쓴다** — 이후 `request.state`가 같은 id로 나간다. envelope `at`이 `request_max_age_sec`(기본 600초 `[예시]`)보다 오래됐으면 답하지 않고 `rejected`로 종결한다 — 엔진이 입력 스트림을 처음부터 읽어 꺼져 있던 동안의 예전 요청이 재기동 때 몰려오기 때문 (#61) | 구현 (`song`은 미구현) |
| `request.cancelled` | `{"request_id", "reason": "listener"\|"operator"}` | `queued`면 큐에서 빼고 `cancelled` 통보. `generating`이면 생성은 끝까지 하되 결과를 버리고 `cancelled` 통보. 이미 제출했으면(`generated`) 엔진은 아무것도 하지 않는다 — 제출된 세그먼트를 빼는 것은 백엔드 몫 | 미구현 |
| `station.created` | 3.3절 | EngineManager가 StationEngine 기동, ack 캐시 사전 렌더링 → `station.started` 또는 `station.rejected`. 같은 `station_id`가 다시 오면(재전달) 무시한다 | 구현 |
| `station.closed` | `{"reason": "normal"\|"operator_stop"}` | 진행 중 생성 취소, 계측 플러시 후 인스턴스 종료 | 구현 (`reason`은 아직 미사용) |
| `track.queued` | 3.4절 — 다음에 틀 곡 정보 | 다음 곡으로 기억해 두고, 러닝오더가 음악 칸에 오면 소개 멘트 생성 (`music_ref`로 그 곡 지정) | 미구현 |
| `track.started` | 3.4절 — 곡 재생 시작 | 곡이 끝나는 시각을 버퍼 계산에 반영 | 미구현 |
| `board.story` | 3.5절 — 게시판 사연 후보 | L0 검사 후 사연 풀에 적재, 사연 코너가 고른다 | 미구현 |
| `board.story.withdrawn` | 3.5절 — `{"story_id"}` 다른 방에서 읽힌 사연 | 사연 풀에서 뺀다 (이미 생성 중이면 결과를 버린다) | 미구현 |
| `backpressure` | `{"d_total_ms", "threshold_ms", "severity"}` | `d_total_ms`를 엔진 버퍼 계산의 기준으로 쓴다 — 보고 시각 이후 흐른 시간만큼 빼고, 그 뒤 엔진이 제출한 분량을 더한다. `target_buffer_sec` 이상이면 생성을 쉰다. 30초 넘게 보고가 없으면 엔진 자체 추정으로 돌아간다. `severity`가 `low`·`critical`이면 `ScheduleContext.backpressure`를 켠다 (#61) | 구현 (filler 우선·요청 보류 같은 정책 반영은 정책 A/B/C에서) |
| `state.transition` | `{"segment_id", "state": "MIXING"\|"PUBLISHED"\|"PLAYED", "at"}` | 재배치 가능 집합에서 제거, 계측 기록. **`PUBLISHED`를 받은 멘트만 "송출됨"으로 기억에 넣는다** (결정 2026-10-08) | 수신만, 무시 (M3) |

상태 전이를 여러 건 보낼 때 **일괄 전용 이벤트는 두지 않는다.** 스트림에 엔트리를 연달아 `XADD`하면 된다.

### 3.3 `station.created` 페이로드

호스트가 방을 만들면 백엔드가 `engine:control`에 넣는 메시지다. 엔진은 이 메시지 하나로 그 방의 DJ를 만들고 방송을 시작한다. envelope은 2장과 같고, `station_id`는 새 방의 ID다. `persona`는 방 생성 화면에서 엔진이 만든 초안(6.2절)을 호스트가 고르고 고친 결과이고, 백엔드 DB에 저장된 값 그대로다. 제한 수치는 `[예시]`다.

```json
{
  "broadcast_minutes": 60,
  "first_song": {"track_ref": "yt:dQw4w9WgXcQ", "title": "밤편지", "artist": "아이유", "duration_ms": 253000},
  "topic": "시험 기간을 버티는 나만의 방법",
  "persona": {
    "persona_id": "psn_7c1e",
    "style": {"formality": "polite", "energy": "low", "humor": "rare"},
    "music_taste": ["발라드", "어쿠스틱"],
    "voice": "<Cartesia 보이스 ID>",
    "dj_name": "새벽",
    "concept": "심야 스터디 라디오",
    "tone": "차분하고 따뜻한 존댓말. 말수가 적고 문장이 짧다",
    "examples": [
      "새벽 한 시가 넘었네요. 아직 책상 앞에 계신 분들, 어깨 한 번 내려놓고 갈게요.",
      "오늘 분량을 다 못 끝내도 괜찮아요. 여기까지 온 것도 꽤 멀리 온 거예요.",
      "졸리면 물 한 잔이요. 저도 지금 한 잔 따라 놨어요."
    ],
    "forbidden": ["큰 소리로 감탄하거나 텐션을 올리기", "공부를 더 하라고 다그치기"],
    "signature_phrases": ["천천히 가요"]
  },
  "policy": "naive_fifo"
}
```

로컬에서 백엔드 대신 넣어 보기 — 엔진은 `onair-engine --transport redis --serve`로 띄운다. `voice`는 `onair-engine --list-voices`의 ID로 바꾼다 (`pipeline.tts: dummy`면 보이스를 검사하지 않는다):

```powershell
wsl redis-cli XADD engine:control '*' type station.created contract_version 1 event_id evt_demo2 station_id st_8f2a at 1789500000 payload '{"broadcast_minutes":30,"persona":{"persona_id":"psn_demo","style":{"formality":"polite","energy":"low","humor":"rare"},"voice":"<Cartesia 보이스 ID>","dj_name":"새벽","concept":"심야 스터디 라디오","tone":"차분하고 따뜻한 존댓말","examples":["새벽 한 시가 넘었네요.","오늘 분량을 다 못 끝내도 괜찮아요.","졸리면 물 한 잔이요."]}}'
wsl redis-cli XRANGE engine:st_8f2a:out - + COUNT 3   # station.started 또는 station.rejected
```

#### 필드

| 필드 | 타입 | 필수 | 없을 때 | 제한 `[예시]` | 엔진이 쓰는 곳 |
|---|---|---|---|---|---|
| `broadcast_minutes` | 정수 | O | — | 30~60 (MVP, 결정 2026-10-08) | 방송 종료 시각, 러닝오더 시간 배분 |
| `first_song` | 곡 정보 (3.4절 `track`) | | 오프닝 곡 없이 바로 오프닝 멘트 | | 방송 시작과 함께 백엔드가 튼다. 엔진은 그동안 오프닝 멘트를 만든다 |
| `topic` | 문자열 | | 엔진이 컨셉에서 생성 | 1~60자 | 오늘의 주제 소개·주제 코너 |
| `persona.persona_id` | 문자열 | O | — | 64자 | 백엔드 DB의 persona ID. 계측 기록에 남긴다 |
| `persona.style.formality` | `polite`\|`casual` | O | — | | 말투 고정, 평가 시 말투 이탈 검사 |
| `persona.style.energy` | `low`\|`mid`\|`high` | O | — | | 말투 |
| `persona.style.humor` | `rare`\|`some`\|`often` | O | — | | 말투 |
| `persona.music_taste` | 문자열 배열 | | `[]` | 0~3개, 고정 장르 목록 중에서 | 곡 소개 멘트의 취향 표현 |
| `persona.voice` | 문자열 | O | — | 엔진 TTS 제공자(Cartesia)의 한국어 보이스 ID 중에서 | TTS 어댑터, ack 사전 렌더링 |
| `persona.dj_name` | 문자열 | O | — | 1~20자 | 시스템 프롬프트 정체성 |
| `persona.concept` | 문자열 | O | — | 1~60자 | 시스템 프롬프트 정체성 |
| `persona.tone` | 문자열 | O | — | 1~100자 | 시스템 프롬프트 말투 |
| `persona.examples` | 문자열 배열 | O | — | 3~5개, 각 150자 | 말투 예시 — 말투를 가장 강하게 고정한다 |
| `persona.forbidden` | 문자열 배열 | | `[]` | 0~10개, 각 50자 | 시스템 프롬프트 금지 사항 |
| `persona.signature_phrases` | 문자열 배열 | | `[]` | 0~3개, 각 30자 | 입버릇 (가끔만 쓰도록 지시) |
| `policy` | 문자열 | | `naive_fifo` | 현재 `naive_fifo`만 | 편성 정책 선택. 코너별 정책 대응표는 [persona.md](persona.md) 8.1절 — 확정 시 객체로 확장 |

- 스키마는 **Pydantic 모델 하나**로 정의하고 백엔드와 엔진이 같이 쓴다 (결정 2026-10-08, [persona.md](persona.md) 2.3절). 이 표가 그 모델의 명세다 — 구현: [packages/onair_schema](../packages/onair_schema/) `StationCreated`·`Persona`.
- **목록은 JSON 배열만** 받는다. 문자열 하나로 오면 거부한다 (한 글자씩 예시로 쪼개지는 사고 방지).
- **필드 이름은 엔진 코드 기준**(`dj_name` 등)이다. 엔진 구현·테스트가 이 이름을 쓴다.
- `running_order`·행동 층(`behavior`)은 v1에 넣지 않는다. 러닝오더는 엔진이 `broadcast_minutes`로 만들고([ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) 4.3절), 정책 A/B/C가 확정되면 필드를 추가하고 `contract_version`을 올린다.
- 엔진은 이 페이로드를 받은 그대로 계측 DB에 남긴다 — 방송 시점의 persona 사본([persona.md](persona.md) 9.4절)이다. 백엔드 DB의 persona가 나중에 바뀌어도 이 방송의 기록은 그대로다.
- 검증에 실패하면 방송을 시작하지 않고 `station.rejected`를 보낸다 (3.1절).

### 3.4 곡 (YouTube 재생, 결정 2026-10-08)

곡을 고르고 재생하는 것은 **백엔드**다 (F-34). 엔진은 곡을 고르지 않고, 백엔드가 알려준 곡을 소개하는 멘트만 만든다. 그래서 엔진의 곡 카탈로그(`catalog.py`)는 로컬 개발·테스트용 더미로만 남는다.

**곡 정보 (`track`)** — 아래 이벤트들과 `first_song`, `request.arrived`(`kind: "song"`)에 같은 형식으로 들어간다.

```json
{"track_ref": "yt:dQw4w9WgXcQ", "title": "밤편지", "artist": "아이유", "duration_ms": 253000}
```

| 필드 | 설명 |
|---|---|
| `track_ref` | 곡 식별자. YouTube 영상이면 `yt:{video_id}` |
| `title`, `artist` | YouTube 메타데이터에서 백엔드가 채운다 (`artist`는 채널명일 수 있음). **외부 텍스트이므로 엔진은 데이터 블록으로만 프롬프트에 넣는다** |
| `duration_ms` | 영상 길이. 엔진의 버퍼·시간 계산에 쓴다 |

**이벤트** (`:in`)

| type | payload | 의미 |
|---|---|---|
| `track.queued` | `{"track": {...}, "source": "playlist"\|"request"\|"vote", "request_id"?}` | 다음에 틀 곡. 백엔드는 **다음 곡을 항상 하나 미리 정해 둔다** — 첫 곡이 시작될 때, 그리고 곡이 시작될 때마다(`track.started` 직후) 그다음 곡을 보낸다. 새 `track.queued`는 이전 것을 대체한다 |

현재 신청곡 등록 경로에서는 음원 큐와 `song_request` 표시 기록을 함께 저장하고,
`track.queued`에 신청자 `nickname`과 `request_id`를 전달한다. 엔진은 닉네임을 L0 검사한 뒤
곡 소개의 데이터 블록에 넣고, 음악 세그먼트의 방송 상태로 신청 기록을 갱신한다.
이는 신청곡에 한정한 연결이다. 아래 v2의 범용 입력 라우터·FEATURE 상태 머신과
신청 한마디 처리는 아직 구현되지 않았다.
| `track.started` | `{"track_ref", "started_at"}` | 곡 재생이 시작됐다. 엔진은 `started_at + duration_ms`를 곡 종료 예상 시각으로 쓴다 |

**역할 나눔** — **어떤 곡**을 틀지는 백엔드가, **언제** 틀지는 엔진 러닝오더가 정한다.

```
백엔드: 다음 곡을 미리 정해 둠 ── track.queued ──→ 엔진: 기억해 둠
엔진: 러닝오더가 음악 칸에 오면 곡 소개 멘트 생성
엔진: segment.submitted (music_ref = track_ref) ──→ 백엔드: 소개 멘트 재생 → 바로 그 곡 재생
백엔드: track.started ──→ 엔진: 곡 끝나는 시각까지 다음 멘트 준비 / 백엔드는 그다음 곡을 track.queued
```

- **첫 곡**(`first_song`)은 예외다. 방송 시작과 함께 백엔드가 소개 없이 바로 튼다. 엔진은 그동안 오프닝 멘트를 만들고, 오프닝에서 "방금 들으신 곡"으로 소개한다 ([ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) 4.3절).
- **대체 규칙** — 신청곡·투표 결과처럼 새 곡이 끼어들면 백엔드는 새 `track.queued`를 보낸다. 다만 엔진이 이미 이전 곡의 소개 멘트를 제출했다면(`music_ref`가 실린 `segment.submitted`를 받았다면) 백엔드는 그 곡을 먼저 틀고, 새 곡은 그다음으로 미룬다 — DJ가 소개한 곡과 실제 곡이 달라지면 안 된다.
- 소개 멘트 생성이 실패하면 엔진은 사전 렌더해 둔 짧은 고정 문구("이어서 한 곡 듣고 올게요" 류)에 `music_ref`를 실어 보낸다 — 곡 재생이 멘트 실패에 막히지 않게 한다 (구현 항목).
- **신청곡 (결정 2026-10-08)** — 사연과 분리된 **신청곡 전용 폼**으로 받는다.

  | 필드 | 필수 | 용도 |
  |---|---|---|
  | 곡 제목 | O | YouTube 검색어 |
  | 아티스트 | | 검색 정확도 — 커버·라이브·동명곡 오탐을 줄인다 |
  | 링크 | | 있으면 검색과 고르기를 건너뛴다 |
  | 한마디 | | DJ가 곡 소개 때 언급한다 → `request.arrived`의 `body` |

  1. 링크가 없으면 백엔드가 제목(+아티스트)으로 YouTube를 검색해 **상위 3개 `[예시]`를 청취자에게 보여주고 고르게** 한다.
  2. 백엔드가 고른 영상을 곡 정보로 풀어 거른다 — 10분 `[예시]` 초과, 재생 불가·연령 제한, 이미 대기 중인 곡과 중복.
  3. 통과하면 `request.arrived`(`kind: "song"`, `track`, `body` = 한마디)를 엔진에 보낸다. **링크는 `body`에 넣지 않는다** — L0의 URL 룰에 걸린다.
  4. 백엔드가 그 곡을 틀 차례가 되면 `track.queued`(`source: "request"`, `request_id`)로 보낸다. 엔진은 곡 소개에서 신청자와 한마디를 언급한다 (반영 ③단계).
  - **프론트 변경 필요**: 지금 신청곡 모달(`SongRequestModal.tsx`)은 `"신청곡: {링크}"`를 사연과 같은 `prompt` 문자열로 보낸다. 위 폼과 검색 결과 고르기 화면으로 바꿔야 한다.
- **선곡 투표** — 마지막 곡은 투표로 정한다. 투표 진행은 프론트·백엔드 기능이고, 엔진은 투표 안내 멘트만 만든다. 결과 곡은 `track.queued`(`source: "vote"`)로 받는다.

### 3.5 게시판 사연 (결정 2026-10-08)

청취자가 **전체 게시판**에 올린 사연 중, 그 방송의 주제에 맞는 것을 골라 사연 코너에서 읽는다. 채팅 요청(`request.arrived`)과 달리 **실시간 반응 대상이 아니다** — 접수 확인(ack)을 보내지 않고, `L_ack`·`L_res` 측정 대상도 아니다.

| type | payload | 엔진 동작 |
|---|---|---|
| `board.story` (`:in`) | `{"story_id", "body", "author_ref", "posted_at", "tags"}` | L0 검사 → 통과하면 그 방의 사연 풀에 적재 |
| `board.story.withdrawn` (`:in`) | `{"story_id"}` | 다른 방에서 읽힌 사연 — 풀에서 뺀다 |

**역할 나눔** — 백엔드가 **후보를 추리고**, 엔진이 **무엇을 언제 읽을지** 고른다.

1. 엔진이 `station.started`로 확정된 주제(`topic`)를 알린다. 호스트가 주제를 비웠으면 엔진이 생성한 값이다 — 그래서 후보 추리기는 `station.started` 이후에 시작한다.
2. 백엔드가 전체 게시판에서 주제와 맞는 사연 **최대 30개** `[예시]`를 골라 `board.story`로 보낸다. 방송 중 새 사연이 올라와 주제와 맞으면 그때 보낸다.
   - **맞추는 방법** `[제안]` — 작성자가 사연을 쓸 때 카테고리·태그를 고르게 하고(예: 공부, 연애, 일상, 위로), 방송 주제와 태그를 맞춘다. 정확도가 더 필요하면 주제와 본문의 임베딩 유사도로 순위를 매긴다.
3. 엔진은 풀에서 **규칙으로** 고른다 — 아직 안 읽은 것, 최근 것, 태그·주제 일치. 게시판 사연은 `story_corner`의 주 소재(FEATURE)다 ([CORNER_DESIGN.md](CORNER_DESIGN.md) 5.4절, 7.5절).
4. 읽은 사연은 그 멘트의 `segment.submitted.request_ref`에 `story_id`가 실려 나간다.

**한 번 읽힌 사연은 다른 방 후보에서 뺀다** (결정 2026-10-08)

- 백엔드는 `story_id`가 실린 멘트의 `PUBLISHED`를 받으면 그 사연을 "읽힘"으로 표시하고, 그 사연을 후보로 보낸 **다른 방들**에 `board.story.withdrawn`을 보낸다.
- 두 방이 거의 동시에 같은 사연을 고르는 경우는 막지 못한다 — 먼저 `PUBLISHED`된 쪽이 이긴다. 막으려면 백엔드에 "읽기 예약" 단계가 필요한데, MVP에서는 받아들인다.
- 사연이 읽히면 백엔드가 작성자에게 "OO 방송에서 읽혔어요"를 알린다 (F-15와 같은 결).
- 전체 게시판이므로 **어떤 방송에서든 읽힐 수 있다는 점을 작성 화면에 알린다.** 개인정보 검사(L0)는 게시 시점에 백엔드가 한 번, 엔진이 받을 때 한 번 한다.
- 사연 본문은 외부 텍스트다 — 프롬프트에는 데이터 블록으로만 들어간다.

### 3.6 BGM과 더킹 (결정 2026-10-08)

**MVP는 "멘트 밑에 별도 BGM을 깔고, DJ가 말할 때만 BGM을 줄이는" 방식만 구현한다.** 곡과 멘트는 지금처럼 번갈아 나가고 겹치지 않는다. 실제 곡 위에 겹쳐 말하는 방식(talk-over)은 MVP 이후로 미룬다 (아래).

```
시간 →
곡      [■■■■■ 곡 A ■■■■■]                                  [■■■■ 곡 B ■■■■]
멘트                       [멘트 1]  [멘트 2]   [멘트 3]
BGM                       [▁▁▁▁▁▁▁▁▃▃▁▁▁▁▁▁▁▁▃▃▁▁▁▁▁▁▁]
                          ↑ 곡이 끝나면 페이드인     ↑ 다음 곡 전에 페이드아웃
                          ▁ 멘트 중 (작게)  ▃ 멘트 사이 (조금 크게)
```

**백엔드가 할 일** (믹싱은 백엔드 담당)

- **BGM이 나가는 구간** — 곡이 끝난 뒤부터 다음 곡이 시작되기 전까지(멘트가 이어지는 구간). 곡이 재생되는 동안에는 BGM을 끈다.
- **더킹** — 멘트가 나가는 동안 BGM을 −15dB `[예시]`로 줄이고, 멘트 사이 빈 구간에서는 조금 올린다. 오르내릴 때 0.3초 `[예시]` 페이드.
  - 엔진이 멘트의 길이(`duration_ms`)와 순서를 정확히 알려주므로, 소리를 감지하는 방식(sidechain)은 필요 없다 — 멘트 구간을 알고 볼륨만 조절하면 된다.
- **BGM 음원** — 저작권 문제가 없는 로열티 프리 연주곡 몇 개를 반복 재생한다. 방 분위기에 맞춰 고를지(예: persona `music_taste`, `style.energy`)는 선택 사항이다.

**엔진이 할 일** — 없다. 기존 `segment.submitted`의 `kind`(`speech`·`ack`·`filler` = 멘트, 곡은 `music_ref`)로 백엔드가 멘트 구간을 구분할 수 있다.

#### MVP 이후: 곡 위에 겹쳐 말하기 (talk-over) — 보류

DJ가 실제 곡의 끝부분·인트로 위에 겹쳐 말하고 그동안 곡 볼륨을 줄이는 연출이다. 실제 라디오에 가깝지만, 백엔드가 곡과 멘트를 정확한 시점에 동시에 섞어야 해서 믹싱 구조 변경이 크다. 보컬 시작 시점을 모르면 인트로 위 멘트가 가수 목소리와 겹치는 문제도 있다. 다시 다룰 때의 출발점:

- 순서 — 곡 **끝부분**에 겹치기(쉬움) → 곡 **인트로**에 겹치기(보컬 시작점 문제).
- 엔진은 `segment.submitted`에 `"overlay": {"track_ref", "anchor": "start"|"end", "lead_ms"}`를 붙이고, 멘트가 겹칠 구간보다 길면 백엔드는 겹치지 않고 차례로 튼다.

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

**Redis 인증 (결정 2026-10-08)** — 단일 VM 배포 전제로, Redis는 `127.0.0.1`에만 바인딩하고 `requirepass`로 비밀번호를 건다. 엔진·백엔드 모두 `ONAIR_REDIS_URL`(`redis://:비밀번호@127.0.0.1:6379/0`) 환경변수로 받는다. MVP에서는 ACL 사용자를 나누지 않는다.

## 5. 오디오 파일

- **공유 오디오 루트**: 엔진 `transport.audio_dir`과 백엔드 설정이 같은 디렉토리를 가리킨다. 단일 머신 배포 전제다.
- **audio_ref**: 루트 기준 POSIX 상대 경로 — `{station_id}/seg_xxx.mp3`, `{station_id}/ack/ack_N.mp3`. 백엔드는 루트 밖으로 벗어나는 경로(`..`, 절대 경로)를 거부하고 파일 존재를 확인한다.
- **곡 음원**: 공유 폴더에 두지 않는다. 백엔드가 YouTube에서 직접 재생한다 (3.4절). 엔진은 곡 오디오에 접근하지 않는다.
- **원자적 쓰기**: 엔진은 같은 디렉토리에 `.{이름}.{랜덤}.tmp{확장자}`로 쓴 뒤 `os.replace`로 교체하고, 교체가 끝난 뒤에만 메시지를 발행한다. 메시지를 받았다면 파일은 완성본이다. 백엔드는 `.`으로 시작하는 파일을 무시한다.
- **포맷**: 제공자 원본 그대로 쓴다(cartesia·google·edge는 mp3, dummy는 wav). 확장자로 구분한다. 샘플레이트와 음량(loudnorm)은 백엔드 FFmpeg가 HLS로 변환할 때 정규화한다.
- **duration_ms**: 엔진이 파일에서 실측한다(mutagen).
- **읽기 전용**: 오디오 파일은 엔진 TTS 캐시와 하드링크로 내용을 공유할 수 있으므로 제자리에서 수정하지 않는다. 삭제는 괜찮다.
- **TTS 캐시**: 엔진 전용이며 공유 루트 밖(`var/tts_cache`)에 있다. 백엔드와 무관하다.

### 5.1 정리 정책 (결정 2026-10-08 — 미구현)

| 대상 | 삭제 주체 | 시점 |
|---|---|---|
| `{station_id}/seg_*` (제출된 세그먼트) | 백엔드 | `PLAYED` 후 24시간 |
| `{station_id}/ack/*` | 백엔드 | 여러 번 재사용되므로 `PLAYED`로 지우지 않는다. 방송 종료 후 24시간이 지나면 디렉토리째 삭제 |
| 제출되지 않은 파일 (생성 취소 등) | 엔진 | 기동 시·주기적으로, 발행 기록에 없고 1시간 이상 된 파일 |
| `.`으로 시작하는 임시 파일 | 엔진 | 비정상 종료 잔여물 — 1시간 이상 된 것 |
| TTS 캐시 | 엔진 | 용량 상한 초과 시 오래 안 쓴 것부터 (LRU) |

## 6. 엔진이 여는 REST — 구현 (#64)

**결정 (2026-10-08)**

- 포트 **8100** `[예시]`, `127.0.0.1`에만 바인딩한다 (단일 VM — 백엔드만 접근).
- 인증: `Authorization: Bearer {ONAIR_ENGINE_TOKEN}`. `/health`만 인증 없이 연다.
- 버전 접두사 `/v1` (`/health` 제외).

### 6.1 조회 (GET)

상태를 바꾸는 호출은 전부 Redis 이벤트로 가고, 스테이션 현재 상태는 하트비트로 간다. 여기 남은 것은 **양이 많아 스트림에 실으면 안 되는 것**(결정 로그)과 전역 집계다. 곡 카탈로그 조회 2개는 YouTube 전환으로 없앴다.

| endpoint | 용도 | 남기는 이유 | 엔진 현황 |
|---|---|---|---|
| `GET /health` | 프로세스 생존 | 하트비트가 끊긴 것이 "엔진 다운"인지 "Redis 문제"인지 가름 | 구현 — `{"ok": true, "stations": n}` |
| `GET /v1/engine/status` | 전역 상태 — 기동 스테이션 수, 동시 생성 여유, 지연 P95 | 하트비트는 스테이션 단위라 전역 합계가 없음 | 구현 — 스테이션별 진행 중 생성·대기 요청 수, 최근 10분 생성 지연(LLM+L2+TTS) P95. **전역** 동시 생성 상한은 아직 없어 방당 상한(`max_concurrent_per_station`)을 보낸다 |
| `GET /v1/voices?native_only=true` | 방 생성 폼의 보이스 선택지 — `{"provider", "voices": [{"id", "name", "gender", "description", "native"}]}`. 기본은 한국어 원어민 보이스만, `false`면 한국어를 말하는 다국어 보이스도 | 보이스 목록은 TTS 제공자에 달려 있고 엔진만 키를 가진다 | 구현 (#64) — 10분 캐시, 더미 TTS면 개발용 보이스 2개 |
| `GET /v1/stations/{stationId}/decisions` | 결정 로그 (생략 결정 포함) | 초당 1.5건 규모라 스트림에 실을 수 없음. 정책 비교 실험의 핵심 데이터 | 구현 — `?after={id}&limit={1~500}` → `{"decisions": [{"id", "at", "context", "decision"}], "next"}`. `next`가 `null`이면 끝. 끝난 방송도 조회된다 |

### 6.2 persona 초안 생성 (POST — 결정 2026-10-08로 추가)

방 생성 화면에서 호스트가 폼을 채우면, 백엔드가 엔진에 persona 초안을 요청한다. 호스트가 화면에서 결과를 기다리는 **동기 요청·응답**이라, 상관 ID와 타임아웃을 따로 관리해야 하는 Redis 대신 REST로 둔다. 방송 흐름(제출·요청·상태)은 그대로 Redis다. 생성 방식은 [persona.md](persona.md) 3절.

| endpoint | 요청 | 응답 |
|---|---|---|
| `POST /v1/personas/drafts` | `{"form": {폼 입력}, "count": 3}` | `200 {"drafts": [persona, ...]}` — 각 초안은 3.3절 `persona` 형식(`persona_id` 제외). `422` 폼 검증 실패, `503` LLM 사용 불가 |
| `POST /v1/personas/check` | `{"persona": {...}}` — 호스트가 고친 최종본 | `200 {"ok": true}` 또는 `422 {"errors": [{"field", "reason"}]}` — 스키마 검증 + 호스트가 고친 텍스트의 L0 검사 + 보이스가 TTS 제공자 목록에 있는지 |

폼 입력(`form`):

```json
{
  "formality": "polite",
  "energy": "low",
  "humor": "rare",
  "music_taste": ["발라드", "어쿠스틱"],
  "voice": "<Cartesia 보이스 ID>",
  "dj_name": null,
  "host_note": "공부하는 사람 옆에 조용히 있어 주는 DJ"
}
```

- 폼은 공용 스키마 [packages/onair_schema](../packages/onair_schema/) `PersonaForm`으로 검증한다 — 백엔드도 같은 모델을 쓸 수 있다. `count`는 1~5 (기본 3).
- 폼 검증 실패는 FastAPI 기본 `422 {"detail": [...]}`, `host_note`의 L0 위반과 모르는 보이스는 `422 {"errors": [{"field": "form.host_note"|"form.voice", "reason"}]}`다. LLM 키 누락·장애·시간 초과·쓸 만한 초안 없음은 `503 {"detail"}`.
- `dj_name`은 선택이다. 비우면 엔진이 초안마다 이름을 제안한다.
- `host_note`는 선택 자유 문장(최대 100자 `[예시]`)이다. 엔진은 L0 검사 후 데이터 블록으로만 프롬프트에 넣는다.
- 응답 시간 목표 15초 이내 `[예시]`. 초과하면 백엔드는 타임아웃 후 재시도 버튼을 보여준다.
- 첫 노래·방송 시간·주제는 persona가 아니라 방 정보라 이 요청에 넣지 않는다 (`station.created`에 직접 들어간다).

### 6.3 REST로 두지 않고 푸시로 처리하는 것

| 노션 표 항목 | 대신 어떻게 | 비고 |
|---|---|---|
| 스테이션 목록·상태 조회 | `station.created`/`station.closed`로 목록을 알고, 상태는 `engine.health` | 백엔드가 자기가 만든 스테이션을 이미 알고 있다 |
| 요청 큐 조회, 요청 상태 조회 | `request.state` 전이를 쌓아 백엔드가 재구성. 대기 건수는 하트비트 `pending_requests` | 상태 머신의 원천은 어차피 엔진 통보다 |
| 러닝오더 조회 | 현재 위치는 하트비트 `order_position`·`next_slot` | 백엔드가 재구성하기 번거로우면 그때 REST로 되돌린다 (결정 2026-10-08) |
| 편성 정책 조회 | 파라미터는 `station.created`, 적용된 이름은 하트비트 `policy_name` | |
| 세그먼트 메타 재조회 | 백엔드가 `segment.submitted`를 자기 큐에 보관 | 엔진은 제출 후 메타를 보관하지 않는다 |

## 7. 노션 "백엔드→엔진" 표 매핑

| 노션 항목 | 결정 |
|---|---|
| 프로세스 헬스 체크 | REST `GET /health` |
| 엔진 전역 상태 조회 | REST `GET /v1/engine/status` |
| 스테이션 기동 | Redis `station.created` (`engine:control`) → `station.started`/`station.rejected` |
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
| 곡 검색/목록, 곡 정보 조회 | **없앰** — 곡은 백엔드가 YouTube에서 고르고, 정보는 `track.queued`로 엔진에 준다 |
| 곡 오디오 다운로드 | **불필요** — 백엔드가 YouTube에서 직접 재생 |
| 러닝오더 조회 | **REST 없음** — 하트비트 `order_position` |
| 편성 정책 조회 | **REST 없음** — `station.created` + 하트비트 `policy_name` |
| 결정 로그 조회 | REST `GET /v1/stations/{stationId}/decisions` |
| (신규) persona 초안 생성 | REST `POST /v1/personas/drafts`, `POST /v1/personas/check` |

### 7.1 이름·표기 (결정 2026-10-08)

| 항목 | 노션 표 | 확정 |
|---|---|---|
| 요청 본문 | `text` | `body` |
| 요청자 식별 | `listener_ref` | `requester_ref` |
| 수신 시각 | payload `received_at` | envelope `at`을 쓰고 payload에는 넣지 않는다 |
| 요청 상태 표기 | `QUEUED`, `GENERATING` (대문자) | 전송은 소문자 고정, 콘솔 표시에서만 대문자 변환 |
| 백프레셔 단위 | `d_total_ms` | `_ms`로 통일 |
| 경로·필드 표기 | 경로 `{stationId}` camelCase | 경로는 camelCase, JSON 필드는 snake_case |

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

## 9. 결정 기록과 남은 미결 사항

### 9.1 2026-10-08 확정 (구 미결 사항 1~11)

| # | 항목 | 결정 |
|---|---|---|
| 1 | 시각 형식 | Unix 초(소수점 포함). envelope `at`과 페이로드 시각 필드 모두 (2장) |
| 2 | `station.created` 페이로드 | persona 본문을 싣는다. 필드는 3.3절 |
| 3 | 소비 그룹 | `:out`은 `backend` 그룹을 백엔드가, `:in`·`engine:control`은 `engine` 그룹을 엔진이 `MKSTREAM`으로 만든다 (1장) |
| 4 | Redis 인증 | `127.0.0.1` 바인딩 + `requirepass`, `ONAIR_REDIS_URL`로 주입 (4장) |
| 5 | `engine.health` | 5초 주기, 필드는 3.1.1. 필드 추가는 버전을 올리지 않는다 |
| 6 | 파일 보존 기간 | 24시간 (5.1절) |
| 7 | 이름·표기 | 7.1절 |
| 8 | REST 포트·인증·버전 | 8100 `[예시]`, `127.0.0.1`, Bearer 토큰, `/v1` (6장) |
| 9 | `request.cancelled` | 3.2절 — 생성 중이면 끝까지 만들고 버린다, 제출 후는 백엔드 몫, 상태 `cancelled` 추가 |
| 10 | 곡 음원 경로·라이선스 | YouTube 전환으로 해당 없음. 곡 정보는 `track.queued` (3.4절) |
| 11 | 푸시 대체 항목 | 그대로 둔다. 백엔드가 재구성하기 번거로운 항목이 생기면 그 항목만 REST로 되돌린다 |

### 9.2 2026-10-08 추가 결정

| 항목 | 결정 |
|---|---|
| 게시판 사연 범위 | 전체 게시판에서 방송 주제에 맞춰 백엔드가 후보를 추린다. 한 번 읽힌 사연은 다른 방 후보에서 뺀다 (3.5절) |
| 플레이리스트 선곡 | 백엔드가 persona `music_taste`를 최대한 반영한다 |
| 선곡 투표 안내 | 종료 15분 전 |
| 다음 곡 | 백엔드가 항상 하나 미리 정해 둔다 (3.4절) |
| 신청곡 | 전용 폼(곡 제목·아티스트·링크·한마디) + 검색 결과 중 고르기 (3.4절) |
| 더킹 | MVP는 멘트 밑 별도 BGM + 더킹만. 곡 위에 겹쳐 말하기는 MVP 이후 (3.6절) |

### 9.3 남은 미결 사항

1. BGM 음원 — 로열티 프리 연주곡 출처, 방 분위기별로 고를지 (3.6절)
2. 게시판 사연과 주제를 맞추는 방법 — 태그 / 임베딩 유사도 (3.5절)
3. 장르 라벨링 방법 — 플레이리스트 선곡과 `music_taste` 매칭에 필요. 비공식 YouTube API는 약관·차단 위험이 있어, 공식 음악 메타데이터 서비스(Last.fm·MusicBrainz 태그) → 없으면 LLM 분류를 권장
4. 예시값 확정 — REST 포트, persona 필드 길이 제한, 초안 응답 목표, 사연 후보 수, 신청곡 검색 결과 수·길이 상한
5. v2 개정안 합의 — 10장. 특히 `input_refs` 전환(10.5), 위치 삽입 지원 여부(10.9)

## 10. v2 개정안 — 코너 재설계 반영 (제안, 백엔드 합의 전)

코너·입력 반영 재설계([CORNER_DESIGN.md](CORNER_DESIGN.md))에 따른 계약 변경 **제안**이다. 1~9장(v1)은 지금 양쪽에 구현된 계약이므로 그대로 두고, 백엔드 파트와 합의하면 v1 본문에 반영한 뒤 `contract_version`을 **2**로 올린다. 이미 구현된 필드의 의미가 바뀌므로(`request.arrived.kind`, `request.state` 값, `segment.submitted.request_ref`) 버전을 올려야 한다.

- **문서 초안과 구현을 나눈다** (PR #101 백엔드 리뷰, 2026-10-12) — 이 장은 방향에 대한 합의이고, 실제 v2 구현은 항목별로 해당 개발 단계에서 이슈로 확정한다. 아직 정하지 않은 세부는 `[확정 전]`으로 표시하고 10.11절에 모았다.
- 백엔드 검토 결과: 재생 시점 구분(10.6)과 v1 유지·v2 분리 방향에 동의. `kind`·`nickname`·`input_refs` 전환 가능. 위치 삽입(10.9)은 구현 가능하나 현재 미지원.

### 10.1 변경 요약

| 이벤트 | 방향 | 변경 |
|---|---|---|
| `station.created` | B→E | `format`, `corners`, `news_sections`, `intensity`, `experiment` 추가. `policy` 제거 (10.2) |
| `request.arrived` | B→E | `kind`를 `chat`\|`message`\|`song_request`로 변경(`story`·`mood` 폐지), `nickname` 추가 (10.3) |
| `request.state` | E→B | state 값을 입력 상태로 교체 (10.4) |
| `segment.submitted` | E→B | `request_ref` 단일 값 → `input_refs` 배열, 입력별 `reflection_levels` 추가, `kind: ack` 폐지 → `glue` (10.5) |
| `segment.started` | B→E | **신설** — 서버 방송 시간상 세그먼트 재생 시작. 입력의 `REFLECTED` 확정과 `L_reflect`·`quote_age` 계산. `state.transition`(`PUBLISHED`·`PLAYED`)은 의미 그대로 (10.6) |
| `corner.changed` | E→B | **신설** — 현재 코너와 입력 계약 요약. 활성화는 기준 세그먼트의 재생 시작 시점 (10.7) |
| `vote.open` / `vote.result` | E→B / B→E | **신설** — 투표 열기 / 집계 결과. 창은 기준 세그먼트의 재생 시작부터 (10.8) |
| `segment.insert_before` | E→B | **신설 `[실험 조건 후보]` `[확정 전]`** — 지연 결합용. M6 별도 작업 (10.9) |
| `board.story` | B→E | 변경 없음. `tags`에 양자택일형 고민 표시 추가 `[미정]` |
| `engine.health` | E→B | `order_position`·`next_slot`·`policy_name` → `corner`·`step`·`experiment`, 풀·inbox 크기 추가 (필드 추가는 버전 무관, 3.1.1) |
| `request.cancelled` | B→E | 풀·inbox에 있으면 빼고 `cancelled`. 이미 `selected`면 제출 전 결과를 통째로 버리고 그 단계를 다시 만든다 (10.1.1) `[확정 전]` |

#### 10.1.1 `selected` 이후 취소 `[확정 전]`

합성된 음성에서 입력 하나만 지울 수는 없다. 그래서 취소된 입력이 들어간 세그먼트는 **세그먼트 단위로** 처리한다.

| 취소 시점 | 엔진 동작 |
|---|---|
| `selected`(생성 중, 제출 전) | 생성이 끝나면 그 결과를 **통째로 버린다**(제출하지 않음). 같은 세그먼트에 함께 들어간 다른 입력은 창이 남아 있으면 풀·inbox로 되돌리고, 그 단계는 취소된 입력을 뺀 채 **한 번 다시 만든다**. 다시 만들 시간이 없으면(버퍼 위험) 인용 없이 만들거나 filler로 대체한다. 취소된 입력은 `cancelled` |
| 제출 후(`segment.submitted` 이후) | 엔진은 아무것도 하지 않는다 (v1 3.2절과 같음). 그 세그먼트를 빼는 것은 백엔드 몫 — `MIXING` 전이면 큐에서 뺄 수 있다. 빼면 함께 들어간 다른 입력도 반영되지 않은 것이므로 백엔드가 `state.transition`으로 알려 주면 엔진이 풀로 되돌린다 `[확정 전: 통보 방식]` |

- 다시 만드는 비용(LLM·TTS 1회)은 취소가 드물다는 전제로 받아들인다. 실측에서 잦으면 "취소된 입력이 든 세그먼트는 그대로 버리고 다시 만들지 않는다"로 줄인다.

### 10.2 `station.created` 추가 필드

```json
{
  "broadcast_minutes": 60,
  "first_song": {"...": "..."},
  "topic": "시험 기간을 버티는 나만의 방법",
  "persona": {"...": "..."},
  "format": "talk",
  "corners": ["story_corner", "topic_talk"],
  "intensity": "mid",
  "experiment": null
}
```

| 필드 | 타입 | 필수 | 없을 때 | 제한 `[예시]` |
|---|---|---|---|---|
| `format` | `music`\|`talk`\|`news` | O | — | |
| `corners` | 문자열 배열 | `talk`만 | — | 2~3개, `story_corner`·`text_corner`·`topic_talk`·`dj_vs_listeners` 중 |
| `news_sections` | 문자열 배열 | `news`만 | — | 2~3개, 종합/경제/IT/스포츠/문화 중 |
| `intensity` | `low`\|`mid`\|`high` | | `mid` | `news`에서는 무시 |
| `experiment` | 객체 \| `null` | | `null` | 실험용 방만. `{"condition", "selector": "recency"\|"relevance"\|"diversity", "window_sec", "binding": "early"\|"late", "aggregate_ack"}` — [persona.md](persona.md) 2.3절 `ExperimentSpec` |
| ~~`policy`~~ | | | | 제거 — `SchedulingPolicy`가 없어진다 |

- 호스트 폼은 형식을 먼저 고르고, 형식에 따라 `corners` 또는 `news_sections`와 `intensity`를 보여준다([CORNER_DESIGN.md](CORNER_DESIGN.md) 8절). `experiment`는 호스트 폼에 노출하지 않는다.
- 검증 실패(형식과 맞지 않는 코너 등)는 v1과 같이 `station.rejected`(`invalid_payload`)다.

### 10.3 `request.arrived`

```json
{"request_id": "req_abc123", "kind": "chat", "body": "저는 새벽에 컵라면이요", "requester_ref": "listener_1", "nickname": "밤샘러"}
```

| `kind` | 보내는 곳 (프론트) | `body` 상한 `[예시]` |
|---|---|---|
| `chat` | 플레이어 채팅창 | 100자 |
| `message` | 플레이어 "사연 보내기" 폼 (v1의 `story`) | 200자 |
| `song_request` | 신청곡 폼 (v1의 `song`) — `track` 필수, `body`는 한마디 | 60자 |

- `nickname`은 방송에서 부를 이름이다(`message`·`song_request` 낭독용). `requester_ref`(내부 ID)는 프롬프트에 넣지 않는다. 사연 보내기 폼에 "방송에서 닉네임이 불릴 수 있음" 동의 표시가 필요하다 `[미정]`.
- `mood`는 폐지한다(기획서 rev.11).
- 투표는 `request.arrived`로 보내지 않는다 — 백엔드가 집계해 `vote.result`로 한 번에 보낸다(10.8).

### 10.4 `request.state` 값

| state | 의미 | 종결 |
|---|---|---|
| `pooled` | 인용 후보 풀 | |
| `inboxed` | 그 입력을 쓰는 코너의 받은편지함 | |
| `collected` | 열린 질문 창에 수집됨 | |
| `held` | 반영하지 않는 구간(곡 재생 중 등) — 다음 코너에서 다시 라우팅 | |
| `not_on_air` | 이 형식은 방송에 반영하지 않음 (뉴스) | O |
| `selected` | 세그먼트에 들어감 (생성 중) | |
| `reflected` | 그 입력이 들어간 세그먼트가 재생되기 시작함 — payload에 `segment_id`, `started_at`(10.6) | O |
| `expired` | 창이 지나도록 선택되지 못함 / 방송 종료 | O |
| `rejected` | 안전 검사 탈락 | O |
| `cancelled` | 취소됨 (v1과 같음) | O |

- `selected` 뒤 생성이 실패하거나 대본에 인용이 빠지면 `pooled`/`inboxed`로 되돌아갈 수 있다. 백엔드는 마지막 상태만 표시하면 된다.
- 프론트 표시 문구 예는 [CORNER_DESIGN.md](CORNER_DESIGN.md) 9.1절.

### 10.5 `segment.submitted`

```json
{
  "id": "seg_9be9401b85d7",
  "corner_type": "topic_talk",
  "kind": "speech",
  "input_refs": ["req_abc123", "req_abc130"],
  "reflection_levels": {"req_abc123": 2, "req_abc130": 3},
  "music_ref": null,
  "...": "나머지 필드는 v1과 같다"
}
```

- `input_refs`: 이 멘트가 반영한 입력의 `request_id` 또는 게시판 사연의 `story_id` 배열. 없으면 `[]`. v1의 `request_ref`를 대체한다 — 채팅 여러 건이 한 멘트에 인용될 수 있기 때문이다.
- `reflection_levels`: **입력별로** 이 멘트가 도달한 반영 단계(1~4, 기획서 4.2절). 같은 멘트라도 입력마다 다르다 — 예: 한 채팅은 인용·반응(②)만, 다른 채팅은 다음 화제가 됨(③, STEER). 키는 `input_refs`와 같고, 입력이 없으면 `{}` (PR #101 리뷰 반영 — 단일 `reflection_level` 대신).
- `kind`: `speech`\|`glue`\|`filler`. v1의 `ack`는 없어지고, 사전 렌더 접착 요소(코너 콜 등)는 `glue`다. 백엔드의 BGM 더킹 구분(3.6절)은 그대로 — 셋 다 멘트다.
- 사연 읽힘 처리(3.5절)는 `request_ref` 대신 `input_refs`에 `story_id`가 있는지로 판단한다.

### 10.6 `segment.started` (B→E, 신설 — PR #99 백엔드 리뷰 반영)

엔진은 입력의 `REFLECTED`를 "그 입력이 들어간 세그먼트의 **재생이 시작된 시각**"에 확정한다(`PUBLISHED`가 아니라). `L_reflect`와 `quote_age`(재생 시작 시각 − 채팅 도착 시각)가 연구 지표이기 때문이다.

```json
{"segment_id": "seg_9be9401b85d7", "started_at": 1789497013.4}
```

| 이벤트 | 의미 | 엔진이 쓰는 곳 |
|---|---|---|
| `state.transition` `PUBLISHED` | HLS에 공개 | 방송 맥락 기록 기준 ([context-memory.md](context-memory.md) 3.1) |
| **`segment.started`** | 서버 방송 시간상 재생 시작 | 입력 `REFLECTED` 확정, `L_reflect`·`quote_age`·`topic_match` 계산 |
| `state.transition` `PLAYED` | 서버 방송 시간상 재생 완료 (기존 의미 유지 — [BACKEND_BROADCAST_RUNTIME.md](BACKEND_BROADCAST_RUNTIME.md)) | 계측(재생 완료 시각) |

- **`PLAYED`의 의미는 바꾸지 않는다.** 처음 제안은 `PLAYED`(완료) 시각에서 `duration_ms`를 빼 시작 시각을 추정하는 것이었지만, (1) 세그먼트가 끝나야 `REFLECTED`가 확정되어 요청자 UI의 "방송에 나왔어요"가 세그먼트 길이(15~45초)만큼 늦어지고, (2) 재생이 끊김 없이 이어졌다는 가정에 기대므로, 시작 시각을 백엔드가 직접 보내는 쪽으로 정했다.
- **측정 기준** — `started_at`은 청취자 기기의 실제 재생 시각이 아니라 **서버 방송 시간 기준**이다. 청취자 간 편차는 E_sync로 따로 잰다.
- **세그먼트 단위다** — 인용 구절이 아니라 그 구절이 들어 있는 **세그먼트의 시작 시각**이다. 인용이 세그먼트 중간에 있으면 `quote_age`가 실제보다 그만큼 작게 잡힌다. 이 한계를 실험 보고에 밝힌다.
- **보내는 범위 (결정 — PR #101 백엔드 리뷰)** — 백엔드는 엔진이 제출한 **모든 세그먼트**에 보낸다. 엔진은 `input_refs`가 빈 세그먼트의 통보는 상태 처리 없이 계측만 남긴다.
- **상태와 지표의 시각을 나눈다** — 엔진은 통보를 **받은 시점에** 입력을 `REFLECTED`로 바꾸고 `request.state`를 보낸다(UI 표시). 그러나 `L_reflect`·`quote_age`·`topic_match`는 받은 시각이 아니라 **페이로드의 `started_at`** 으로 계산한다 — 스트림 지연·재기동 후 몰아 받기가 지표에 섞이지 않게.
- **재전달·중복 (at-least-once, 4장)** — 엔진은 `segment_id` 단위로 멱등하게 처리한다. 같은 세그먼트의 통보가 다시 오면 처음 받은 `started_at`을 유지하고 상태 전이·계측을 다시 하지 않는다(ACK만). 입력이 이미 `REFLECTED`면 다른 세그먼트의 통보로 다시 전이하지 않는다 — 반영 횟수 집계는 입력당 첫 재생 한 번이다. 백엔드도 같은 세그먼트에 `started_at`을 바꿔 다시 보내지 않는다.

### 10.7 `corner.changed` (E→B, 신설)

```json
{"corner": "topic_talk", "title": "오늘의 이야기", "contract": {"chat": "quote", "message": "defer", "song_request": "defer"}, "segment_id": "seg_4b1f0c2a9e11"}
```

- **방송 시점에 맞춘다 (PR #101 백엔드 리뷰)** — 엔진은 코너를 실제 방송보다 앞서 편성하므로, 엔진이 코너에 들어간 시점에 화면을 바꾸면 청취자는 아직 앞 코너를 듣고 있다. 그래서 엔진은 `corner.changed`에 **그 코너의 첫 세그먼트 id**(`segment_id`, 보통 코너 콜)를 싣고, 그 세그먼트를 제출하기 **전에** 보낸다. 백엔드는 이 값을 들고 있다가 그 세그먼트의 **재생이 시작될 때**(10.6과 같은 시점) 프론트에 전달하고 현재 코너로 바꾼다 `[확정 전: 기준 세그먼트가 폐기·교체될 때 처리]`.
- 백엔드는 WebSocket으로 프론트에 전달하고, 늦게 들어온 청취자에게도 마지막으로 **활성화된** 값을 보낸다(기획서 5.6절 늦은 합류).
- 입력 라우팅은 엔진 기준 코너로 한다 — 화면 표시와 엔진 계약이 잠깐(버퍼 길이만큼) 어긋날 수 있다. 이 사이에 들어온 입력은 엔진이 다음 코너 계약으로 라우팅한다 `[확정 전: UI 안내 문구]`.
- 프론트는 `contract`로 "지금은 채팅이 인용될 수 있는 시간", "사연은 문자 소개 시간에 읽혀요" 같은 안내를 띄운다. 값은 `quote`\|`steer`\|`read`\|`feature`\|`solicit`\|`closed`\|`defer`.

### 10.8 `vote.open` / `vote.result` (신설)

```json
// vote.open (E→B)
{"vote_id": "vote_01", "kind": "game", "question": "고백은 문자로? 전화로?", "options": ["문자", "전화"], "segment_id": "seg_7a0d3e5f1c22", "window_sec": 60}
// vote.result (B→E)
{"vote_id": "vote_01", "counts": {"문자": 12, "전화": 7}, "total": 19}
```

- `kind`: `game`(`dj_vs_listeners` 코너) \| `last_song`(엔딩 선곡 투표, F-36). `last_song`의 결과 곡은 v1처럼 `track.queued`(`source: "vote"`)로도 보낸다.
- **창은 방송 시점 기준이다 (PR #101 백엔드 리뷰)** — 엔진은 열 시각(`closes_at`)을 정할 수 없다. 대신 **기준 세그먼트**(`segment_id`, 선택지를 소개하는 멘트)와 창 길이(`window_sec`)를 보낸다. 백엔드는 그 세그먼트의 재생이 시작될 때 투표를 열고 `window_sec` 뒤에 닫아 `vote.result`를 보낸다. 엔진은 결과를 받은 뒤에 결과 공개 멘트를 만든다 `[확정 전: 기준 세그먼트가 폐기될 때 처리, 결과가 늦을 때 엔진 대기 상한]`.
- 한 청취자 한 표 — 중복 방지는 백엔드가 `requester_ref`로 한다. `vote.result`가 재전달되면 엔진은 `vote_id`로 한 번만 반영한다.
- 엔진은 투표 창이 열린 동안 방송이 비지 않도록 다른 단계(DJ 이야기, 곡)와 겹쳐 연다. DJ의 선택은 결과를 보기 전에 정해 저장한다.
- 투표한 청취자의 입력 상태(`collected`)는 백엔드가 직접 표시해도 된다 — 엔진이 표마다 `request.state`를 보내지 않는다.

### 10.9 `segment.insert_before` (E→B, 신설 `[실험 조건 후보]` `[확정 전 — M6 별도 작업]`)

지연 결합(RQ-B 실험 조건)용이다. 엔진이 5~10초짜리 인용 세그먼트를 만들어, 백엔드 큐에서 아직 `MIXING`에 들어가지 않은(`GENERATED`) 세그먼트 **앞에** 끼워 달라고 요청한다.

```json
{"segment": {"...": "segment.submitted와 같은 형식"}, "before_segment_id": "seg_9be9401b85d7"}
```

- **백엔드 검토 (PR #101)** — 구현 가능하지만 현재 미지원. 큐에서 다음 세그먼트를 고르는 것과 삽입이 동시에 일어나지 않게 제어해야 하므로, **수락·거절 응답과 중복 처리 계약을 먼저 정하고 M6에서 별도 작업으로** 진행한다.
- 거절되면 엔진은 그 인용 세그먼트를 **버린다**(늦게 나간 인용은 의미가 없다 — CORNER_DESIGN U13). 입력은 창이 남아 있으면 풀로 되돌린다.
- 응답 계약 초안 `[확정 전]` — 백엔드가 `segment.insert_result {"segment_id", "accepted": true|false, "reason"}`(B→E)를 보낸다. 같은 `segment.id`의 삽입 요청이 재전달되면 백엔드는 처음 결과를 다시 보내고 두 번 넣지 않는다. 엔진은 응답이 오기 전까지 그 입력을 `selected`로 두고, 일정 시간(예: 5초 `[예시]`) 안에 응답이 없으면 거절로 본다.
- 이 이벤트가 생기기 전에는 RQ-B를 버퍼 크기 단일 변수(선결합만)로 진행한다(기획서 10장).

### 10.10 엔진 구현 순서

v2 합의 전까지 엔진은 v1을 유지한다. 합의 후 [ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) 9장 M3~M5에서 다음 순서로 바꾼다.

1. `station.created` 추가 필드 (M3 — 형식·클락)
2. `request.arrived` `kind`·`nickname`, `request.state` 새 값, `segment.submitted` `input_refs`·`reflection_levels`·`kind: glue`, `segment.started` 처리, `state.transition` `PUBLISHED` 처리(방송 맥락), `corner.changed`, `selected` 이후 취소 (M4 — 입력 반영)
3. `vote.open`/`vote.result` (M5)
4. `segment.insert_before`·`segment.insert_result` (M6 — 별도 작업)

- 단계마다 이 장에서 해당 항목을 확정하고 GitHub 이슈로 백엔드와 맞춘다. 확정하면 `[확정 전]` 표시를 지우고 v1 본문(1~9장)으로 옮긴다.

### 10.11 확정 전 항목 (PR #101 백엔드 리뷰 기준)

| # | 항목 | 정할 때 | 관련 |
|---|---|---|---|
| C1 | `selected` 이후 취소 — 제출 전 결과 폐기·재생성 범위, 제출 후 백엔드가 세그먼트를 뺐을 때 엔진 통보 방식 | M4 | 10.1.1 |
| C2 | 입력별 반영 단계 `reflection_levels` 형식 | M4 | 10.5 |
| C3 | `corner.changed` — 기준 세그먼트가 폐기·교체될 때 처리, 화면과 엔진 계약이 어긋나는 동안의 UI 안내 | M4 | 10.7 |
| C4 | `vote.open` — 기준 세그먼트가 폐기될 때, 결과가 늦을 때 엔진 대기 상한 | M5 | 10.8 |
| C5 | `segment.insert_before` — 수락·거절 응답(`segment.insert_result`), 중복 처리, 큐 선택과의 동시성 제어 | M6 | 10.9 |

결정된 것: `segment.started`는 모든 세그먼트에 보내고 입력 없는 통보는 엔진이 무시한다, 상태는 받은 시점에 바꾸되 지표는 `started_at`으로 계산한다, 재전달은 `segment_id` 단위로 멱등 처리한다(10.6).
