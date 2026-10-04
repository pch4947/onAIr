# 컨텍스트 유지 설계

> **한 줄 요약** — **실제로 송출된 것**만 채널(방)별 하나의 기록에 남기고, 그 기록에서 **프롬프트용 요약**과 **분석용 로그** 두 개의 뷰를 뽑는다. 프롬프트에는 전체 기록이 아니라 요약 + 최근 N개만 넣는다.

- 관련 문서: [persona.md](persona.md) (페르소나 3층 중 기억 층), [PROJECT_PLAN.md](PROJECT_PLAN.md) (기획서 rev.9 — 4.2 요청 반영의 정의, 5.1 persona·context 관리, 8장 연구 설계 초안), [ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) (4.4 방송 맥락, 7장 계측), [ENGINE_REDIS_CONTRACT.md](ENGINE_REDIS_CONTRACT.md)
- 표기: `[예시]` = 팀 합의 전 예시값, `[미정]` = 결정되지 않은 선택, **권장 이름** = 코드에 아직 없는 이름
- 코드 기준: `origin/feat/#28-engine-llm-pipeline` 브랜치(미머지, 2026-10-02 확인). main에 없는 항목은 따로 표시한다.

## 현재 구현 상태

| 항목 | 상태 | 근거 (코드) |
|---|---|---|
| 직전 멘트를 프롬프트에 넣기 (Working) | **부분 구현** | [pipeline/pipeline.py](../apps/engine/src/onair_engine/pipeline/pipeline.py) `GenerationPipeline._recent: deque[str]`(maxlen = `StationConfig.recent_segments`, 기본 8)와 `_with_context()`. **feat/#28 브랜치에만 있음.** 요약 없이 대본 원문을 그대로 넣는다 |
| 송출 이력 (Episodic) | **미구현(설계)** | `AiredHistory` 류 없음. `_recent`는 **TTS 완료 시점**에 기록하며 메모리에만 있다 |
| 기록 기준 = 실제 송출 | **미구현(설계)** | 백엔드 → 엔진 `state.transition`(`PLAYED`) 이벤트는 계약에 정의돼 있으나 엔진은 "수신만 하고 무시" ([ENGINE_REDIS_CONTRACT.md](ENGINE_REDIS_CONTRACT.md) 3.2, M3) |
| 결정 로그 | **구현됨** | [telemetry.py](../apps/engine/src/onair_engine/telemetry.py) `Telemetry` — SQLite `decision_log`, `request_log`, `generation_log` |
| 요청 기록 | **부분 구현** | `ListenerRequest(request_id, kind, body, requester_ref, received_at, state)` ([domain.py](../apps/engine/src/onair_engine/domain.py)) + `request_log`(상태 전이 시각). 반영 단계(①~④) 시각·선별 여부 없음 |
| 화자별 관계 요약 (Affective) | **미구현(설계)** | — |
| 요약 갱신 | **미구현(설계)** | — |
| 채널별 독립 | **구현됨** | `GenerationPipeline`이 스테이션마다 생성되고, `Telemetry`는 모든 행에 `station_id`를 남긴다 |
| 기억 텍스트의 데이터 블록 분리 | **미구현(설계)** | `_with_context()`가 직전 대본을 system 프롬프트에 그대로 붙인다 |
| 행동 로깅·채팅 분류 | **미구현(설계)** | — |
| persona·context 영속 저장소 | **미구현(설계)** | `[미정]` ([persona.md](persona.md) 2.4절) |

---

## 1. 문제

- **LLM 호출은 무상태다.** 기록을 넘기지 않으면 같은 뉴스를 세 번 읽고, 매번 같은 오프닝을 쓴다. feat/#28 브랜치의 `_recent`가 "실 LLM이 매번 같은 인사로 시작하지 않게" 하려고 들어간 것이 이 문제의 첫 대응이다.
- **공유 스트림은 반복이 그대로 드러난다.** 모든 청취자가 같은 방송을 길게 듣기 때문에, 개인 대화형 서비스에서는 눈에 띄지 않을 반복도 여기서는 모두에게 들린다.

## 2. 기억 4층과 onAIr 대응

| 층 | 내용 | onAIr 구현 | 범위 |
|---|---|---|---|
| **Working** | 현재 대화·직전 멘트 | 프롬프트 컨텍스트 (요약 + 최근 N개) | MVP |
| **Episodic** | 실제로 **송출된** 멘트·곡·소재 + **받은 요청·화자·도달 반영 단계** | 송출 이력 — **권장 이름** `AiredHistory` (현재 `_recent`를 확장·대체) | MVP |
| **Semantic** | 캐릭터 설정·사실 | persona 명세([persona.md](persona.md)) + 소스 피드(RSS, [sources/rss.py](../apps/engine/src/onair_engine/sources/rss.py)) | MVP |
| **Affective** | **화자별 관계 요약** — 이번 방송의 요청 수, 최근 화제, 마지막 반영 시각 | 신규 — **권장 이름** `SpeakerSummary` | **세션 내만** (MVP) |

- **세션 간 장기 기억은 범위 밖이다** (F-23, COULD). 엔진 설계도 이미 "방송 간 장기 기억은 두지 않는다"로 정했다 (ENGINE_ARCHITECTURE 4.4, 확인 10). 참고로 Neuro-sama도 확인된 세션 간 장기 기억이 없다 (AI VTuber 선행 조사).
- 기억 관리 프레임워크(예: Letta)는 **검토 후보**로만 둔다. 현재 요구(세션 내, 채널별, 요약 + 최근 N개)는 엔진 안의 작은 모듈로 충분할 수 있다 `[미정]`.

## 3. 핵심 규칙

### 3.1 기록 기준은 "생성"이 아니라 "실제 송출"이다

- 생성됐지만 큐에서 교체되어 나가지 않은 세그먼트를 기록하면, **방송에 나가지 않은 소재를 이미 다룬 것으로 보고 걸러내게 된다.** 반대로 청취자는 들은 적 없는 내용을 DJ가 "아까 말씀드린"이라고 부를 수도 있다.
- **현재 어긋남** — `_recent`는 TTS 직후(`GenerationPipeline._finish`)에 추가된다. 재배치·취소(ENGINE_ARCHITECTURE 4.1)가 구현되면 이 차이가 실제 문제가 된다.
- **필요한 것** — 백엔드의 `state.transition` 이벤트(`{"segment_id", "state": "PLAYED", "at"}`)를 엔진이 받아 처리해야 한다. 현재 계약에는 정의만 있고 엔진은 무시한다(M3). `PUBLISHED`와 `PLAYED` 중 어느 시점을 "송출"로 볼지는 `[미정]` — 늦은 합류 청취자를 고려하면 `PUBLISHED`(세그먼트 공개)가 "방송에 나갔다"는 의미에 더 가깝다.
- 송출 확인 전까지 생성 완료 세그먼트는 **권장 이름** `pending_air` 상태로 따로 들고 있다가, 송출 이벤트가 오면 이력으로 옮긴다. 교체·취소되면 버린다.

### 3.2 기억 층과 결정 로그는 원천이 같다

- 기억 층(무엇이 나갔고 누가 무엇을 요청했나)과 결정 로그(편성 관리자가 무엇을 골랐나)는 같은 사건의 기록이다. **저장소를 따로 두지 않는다.**
- **하나의 기록에서 두 개의 뷰를 뽑는다.**
  - **프롬프트용 요약** — 요약 + 최근 N개 (4절)
  - **분석용 로그** — 반영 단계 도달 시각, 선별 여부 등 (5·6절)
- 현재 `Telemetry`의 SQLite가 분석용 로그 쪽 원천이다. 송출 이력을 같은 저장소의 테이블로 두면 이 원칙과 맞는다 — 다만 저장소 자체는 `[미정]`이다 ([persona.md](persona.md) 2.4절).

### 3.3 소비자는 둘이다

| 소비자 | 쓰는 것 | 용도 |
|---|---|---|
| **편성 관리자** (`Scheduler`, 코너의 `gather`) | 최근 송출 곡·뉴스 항목 ID, 화자별 마지막 반영 시각 | 방금 튼 곡이나 읽은 뉴스를 다시 고르지 않기, `flood_selection`의 `not_yet_reflected_this_session` 계산 |
| **ScriptWriter** (권장 이름, 현재 `_with_context`) | 요약 + 최근 N개 멘트 | 시스템 프롬프트 6번 블록 ([persona.md](persona.md) 4절) |

- 현재 `Catalog.pick()`은 단순 순환이고, `BriefingCorner.gather()`는 항상 RSS 최신 3개를 읽는다 — 둘 다 송출 이력을 보지 않는다.

### 3.4 기록은 채널(방)별로 독립이다

- 한 방의 기억이 다른 방 프롬프트에 섞이면 안 된다. 스테이션마다 `GenerationPipeline`이 따로 생성되는 현재 구조(`StationEngine`)를 유지하고, 송출 이력도 `station_id` 단위로 둔다.

## 4. 컨텍스트 예산

- 프롬프트에는 전체 기록이 아니라 **요약 + 최근 N개**만 넣는다.
  - N: `[예시]` 8 — 현재 `StationConfig.recent_segments` 기본값. ENGINE_ARCHITECTURE 4.4는 "프롬프트 길이·대본 품질 실측 후 조정"으로 정해 두었다.
  - 요약 길이 상한: `[미정]` (기획서 5.1 "누적 context는 요약하고 길이를 제한한다")
- **비용과 생성 지연이 함께 줄어든다.** 프롬프트 길이에 비례해 입력 토큰 비용과 생성 지연이 늘기 때문이다. 버퍼 기준값을 단계별 실측으로 정하는 만큼(기획서 5.5, F-31), 프롬프트 길이도 그 실측 조건에 포함한다.
- **요약 갱신 — 제안**
  - **언제**: 코너 경계마다 `[예시]`. 코너 경계는 편성 관리자가 이미 아는 지점이고, 한 코너 안의 멘트는 최근 N개로 덮인다.
  - **누가**: 백그라운드 저비용 LLM 호출 `[예시]`. 방송 생성 경로(`GenerationPipeline.run`)와 분리해, 요약이 늦어도 대본 생성이 기다리지 않게 한다. 요약이 아직 없으면 이전 요약을 그대로 쓴다.
  - **무엇을**: 최근 N개 밖으로 밀려난 멘트 + 이번 코너에서 다룬 요청·화제. 실패해도 방송은 멈추지 않는다(요약 없이 최근 N개만 사용).
- **저장 후 재사용** — persona와 요약된 context는 저장해 두고 재사용한다 (1차 멘토링 멘토). 엔진 재기동 시 같은 방송을 이어갈 때 요약을 다시 만들지 않기 위해서다.

## 5. 반영 ④단계를 위한 데이터

요청마다 다음 필드를 기록한다. 이 기록이 ④단계(재언급)의 재료이자, `L_ack`·`L_res` 산출과 RQ2 (a)/(b) 집단 분류의 원천이다(F-30).

| 필드 | 의미 | 현재 코드 |
|---|---|---|
| `request_id` | 요청 ID (백엔드 발급) | `ListenerRequest.request_id` — 있음 |
| `speaker_id` | 화자 | `ListenerRequest.requester_ref` — **있음 (이름 다름).** 새 이름을 만들지 않고 `requester_ref`를 쓰는 것을 권장 |
| `text` | 요청 본문 | `ListenerRequest.body` — **있음 (이름 다름).** `body` 유지 권장 |
| `received_at` | 수신 시각 | `ListenerRequest.received_at` — 있음 |
| `kind` (proactive \| reactive) | 방송 흐름을 바꾸려는 요청인지(proactive), 이미 나온 내용에 반응한 것인지(reactive) | **충돌** — 기존 `ListenerRequest.kind`는 `"story" \| "mood"`(요청 종류)이고 백엔드 계약에도 쓰인다. **권장 이름** `intent`로 분리 |
| `selected` | 폭주 시 선별에 포함됐는지 — false면 RQ2 (b) | 없음. 생략 여부는 `decision_log`의 `Decision`에서 간접 추정만 가능 |
| `ack_at` | ① 접수 확인 송출 시각 | 없음. ack 세그먼트 제출은 있으나 송출 시각은 백엔드 이벤트 필요 |
| `reacted_at` | ② 리액션 송출 시각 | 없음 |
| `schedule_changed_at` | ③ 흐름 변경 송출 시각 | 없음 |
| `recalled_at` | ④ 재언급 송출 시각 | 없음 |
| `max_level_reached` | 도달한 최고 단계 (0~4) | 없음 |

- `*_at` 필드는 모두 **송출 시각**이다(3.1). 엔진은 세그먼트 제출 시 `request_ref`를 붙이므로(`SegmentSubmission.request_ref`), 백엔드의 `state.transition`(`segment_id`, `at`)과 조인하면 계산할 수 있다.
- **화자별 요약(Affective)은 이 기록에서 파생한다.** 별도 저장소를 두지 않고, `requester_ref`로 묶어 "이번 방송의 요청 수, 최근 화제, 마지막 반영 시각"을 계산한다.
- `intent`(proactive/reactive)를 누가 판정할지(LLM 분류, 규칙, 백엔드 입력 UI) `[미정]`.
- ④단계 생성: 편성 관리자가 송출 이력에서 재언급할 요청을 골라 job에 담는다(`max_reflection_level` ≥ 4일 때). LLM이 기억에서 알아서 꺼내게 하지 않는다 — "편성은 규칙, LLM은 문장만" ([persona.md](persona.md) 1.1).

## 6. 로깅 지표 (멘토: 설문보다 로깅)

설문 응답은 누락·성의 없는 응답으로 흔들리므로, 로깅 기반 지표를 1차 근거로 둔다 (1차 멘토링 멘토, 기획서 10장 "설문 응답 신뢰도").

- **행동 지표** (F-32)
  - 요청 후 이탈 시간
  - 재요청 빈도
  - 채팅 빈도 변화
  - 청취 유지 시간
  - 수집 위치는 웹 플레이어(기획서 9장)이며, 엔진 기록과는 `request_id`·`requester_ref`로 조인한다.
- **채팅 6범주 분류** — 긍정 / 부정 / 질문·명령 / 일반 반응 / 시청자 간 대화 / 기타
  - LLM으로 분류하고, **무작위 100건은 사람이 따로 분류해 Cohen's κ로 신뢰도를 보고**한다.
  - 분류는 사후 분석으로 돌리고 방송 생성 경로에는 넣지 않는다 `[예시]` — 생성 지연에 영향을 주지 않기 위해서다.
- **설문** (F-33) — 곡 전환 구간에 **요청자 개인 UI 오버레이로만** 띄운다. 공유 방송은 한 사람의 응답을 기다릴 수 없다. 멘토가 제안한 "응답해야 다음으로" 방식은 채택하지 않는다 (기획서 6.2 F-33 설계 메모).

## 7. 안전과 기억

- **기억에 저장된 청취자 텍스트도 프롬프트에 다시 들어갈 때 데이터 블록으로 분리한다.** 과거 채팅을 통한 **지연된 인젝션**을 막기 위해서다. 요청 당시에는 무해해 보이던 문장이, 나중에 요약·재언급으로 시스템 프롬프트에 들어가면 지시로 해석될 수 있다.
  - 현재 `_with_context()`는 직전 대본 원문을 system 프롬프트에 그대로 붙인다. 대본은 LLM 산출물이지만 청취자 사연을 인용할 수 있으므로, 같은 데이터 블록 규칙을 적용한다 ([persona.md](persona.md) 4절 8번).
  - 요약 생성 호출에도 같은 규칙을 적용한다. 요약 결과는 L2 출력단 검사를 거친 뒤 저장한다 `[예시]`.
- **거부(REJECTED)된 요청은 기억 요약에 넣지 않는다.** L0~L2 어느 단계에서 거부됐든 송출되지 않았으므로 3.1의 기준으로도 기록 대상이 아니다. 분석용 로그(`request_log`)에는 거부 상태가 남는다.
- 안전 계층은 기억 층의 어떤 내용으로도 우회되지 않는다 ([persona.md](persona.md) 5.1).
