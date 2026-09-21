# 엔진 ↔ 백엔드 통신 계약 (초안 v1)

- 상태: **흐름(Redis)은 엔진 구현 완료, 조회(REST)는 미구현. 백엔드 담당 합의 전 초안.** 필드나 의미를 바꾸면 `contract_version`을 올린다.
- **결정 (2026-09-20, 확인 1)**
  - **흐름은 Redis Streams** — 세그먼트 제출, 요청 전달, 상태 통보, 스테이션 수명주기
  - **조회는 엔진이 여는 REST (GET 전용)** — 운영자 콘솔·디버깅용
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
| `engine.health` | 버퍼 잔량 초, 진행 중 생성 수, 최근 지연 P95 | 예정 — 조회 REST(6장)와 중복이므로 필요 여부 합의 |

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

### 3.2 백엔드 → 엔진 (`:in`, `station.created`만 `engine:control`)

| type | payload | 엔진 동작 | 상태 |
|---|---|---|---|
| `request.arrived` | `{"request_id", "kind": "story"\|"mood", "body", "requester_ref"}` | L0 검사 후 요청 큐 적재. **request_id는 백엔드 발급값을 그대로 쓴다** — 이후 `request.state`가 같은 id로 나간다 | 구현 |
| `station.closed` | `{"reason": "normal"\|"operator_stop"}` | 진행 중 생성 취소, 계측 플러시 후 인스턴스 종료 | 구현 (`reason`은 아직 미사용) |
| `station.created` | `{"station_id", "profile": {dj_name, tone, concept, broadcast_minutes}, "policy", "running_order"}` | EngineManager가 StationEngine 기동, ack 캐시 사전 렌더링 | M3 |
| `request.cancelled` | `{"request_id", "reason": "listener"\|"operator"}` | 대기 큐에서 제거, 생성 중이면 결과 폐기 | **미구현 — 신규 합의 항목** |
| `backpressure` | `{"d_total_ms", "threshold_ms", "severity"}` | 정책 ScheduleContext에 반영 → filler 우선 생성·요청 반영 보류 | 수신만, 무시 (M3) |
| `state.transition` | `{"segment_id", "state": "MIXING"\|"PUBLISHED"\|"PLAYED", "at"}` | 재배치 가능 집합에서 제거, 계측 기록, 방송 맥락 갱신 | 수신만, 무시 (M3) |

상태 전이를 여러 건 보낼 때 **일괄 전용 이벤트는 두지 않는다.** 스트림에 엔트리를 연달아 `XADD`하면 된다.

## 4. 전달 보장

- **at-least-once.** 엔진은 이벤트 처리가 끝난 뒤 `XACK`한다. 엔진이 처리 도중 죽으면 재기동 시 자기 consumer의 pending(ID `0`)부터 다시 처리한다. 백엔드는 `event_id`로 중복을 무시할 수 있어야 한다.
- 엔진 consumer 이름은 `engine-{hostname}`이다. 재기동해도 같은 이름이어야 pending을 되찾는다.
- 계약 위반 메시지(필드 누락, JSON 오류)는 엔진이 `TRANSPORT_BAD_EVENT` 로그를 남기고 ACK한다. 붙잡고 있으면 뒤 메시지가 전부 막히기 때문이다.
- 엔진 발행이 실패하면(Redis 다운) `TRANSPORT_FAILED` 로그만 남기고 방송을 계속한다. 재제출 큐는 M2.
- 엔진의 이벤트 수신 루프는 연결이 끊기면 3초 뒤 다시 구독한다.

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

**GET만 둔다.** 상태를 바꾸는 호출은 전부 Redis 이벤트로 가므로 이 API는 운영자 콘솔·디버깅용 읽기 전용이다.
이 결정으로 엔진이 포트를 하나 열게 된다(설계 문서 6장 갱신). 내부망 전제이며 인증 방식은 미결(9장).

| endpoint | 용도 | 엔진 현재 상태 |
|---|---|---|
| `GET /health` | 프로세스 생존 | 신규 (간단) |
| `GET /v1/engine/status` | 기동 스테이션 수, 동시 생성 여유, 최근 생성 지연 P95 | **부분** — 스테이션 수·진행 중 생성 수는 있음. P95 집계(`generation_log`)와 **전역** 동시 생성 상한은 없음 (현재는 스테이션별 설정) |
| `GET /v1/stations` | 기동 중인 인스턴스 목록 | 있음 (`EngineManager._stations`) |
| `GET /v1/stations/{stationId}` | 버퍼 잔량, 진행 중 생성 수, 러닝오더 위치, 대기 요청 수 | 있음 (`Scheduler._snapshot`) |
| `GET /v1/stations/{stationId}/requests` | 대기 요청 목록 + 각 대기 시간 | 있음 (`Scheduler._pending`, `received_at`) |
| `GET /v1/stations/{stationId}/requests/{requestId}` | 요청 현재 상태 | **부분** — 진행 중은 메모리, 끝난 요청은 `request_log`(SQLite) 조회 필요 |
| `GET /v1/stations/{stationId}/running-order` | 코너 슬롯 배열과 현재 위치 | 있음 (`RunningOrder.slots`, `.position`) |
| `GET /v1/stations/{stationId}/policy` | 현재 적용 정책과 파라미터 | 있음 (`policy_name`, `target_buffer_sec`, `max_concurrent_generations`) |
| `GET /v1/stations/{stationId}/decisions` | 결정 로그 (생략 결정 포함) | 있음 (`decision_log`) — 페이지네이션 필요 |
| `GET /v1/catalog/tracks` | 곡 목록·검색 | 있음 (더미 3곡) |
| `GET /v1/catalog/tracks/{trackId}` | 곡 제목·아티스트·길이·무드 | **부분** — 라이선스 필드 없음 |
| `GET /v1/segments/{segmentId}` | 세그먼트 메타 재조회 | **없음** — 엔진은 제출 후 메타를 보관하지 않는다. 백엔드가 자기 큐에 보관하면 불필요 — **합의 항목** |

## 7. 노션 "백엔드→엔진" 표 매핑

| 노션 항목 | 결정 |
|---|---|
| 프로세스 헬스 체크 | REST `GET /health` |
| 엔진 전역 상태 조회 | REST |
| 스테이션 기동 | Redis `station.created` (`engine:control`) |
| 스테이션 목록·상태 조회 | REST |
| 스테이션 종료 | Redis `station.closed` (`reason`) |
| 요청 전달 | Redis `request.arrived` |
| 요청 큐·상태 조회 | REST |
| 요청 취소 | Redis `request.cancelled` — 엔진 미구현 |
| 백프레셔 통보 | Redis `backpressure` |
| 세그먼트 상태 전이 통보 | Redis `state.transition` |
| 상태 전이 일괄 통보 | **불필요** — 스트림에 연달아 XADD |
| 다음 세그먼트 가져오기 | **불필요** — 엔진이 `:out`으로 밀어준다 |
| 세그먼트 메타 재조회 | REST (선택, 6장 참고) |
| 세그먼트 오디오 다운로드 | **불필요** — 공유 폴더 경로 |
| 곡 검색/목록, 곡 정보 조회 | REST |
| 곡 오디오 다운로드 | **불필요** — 공유 폴더 `music/` |
| 러닝오더·정책·결정 로그 조회 | REST |

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
2. `engine:control`의 `station.created` 페이로드 — StationProfile 필드, 정책 설정, 러닝오더 파라미터
3. 백엔드 소비 그룹 이름과 생성 주체
4. Redis 인증 — 비밀번호/ACL은 `ONAIR_REDIS_URL` 환경변수(`redis://:pw@host:6379/0`)로 주입
5. `engine.health` 이벤트를 둘지, 조회 REST(`/v1/engine/status`)로 대체할지
6. 5.1 정리 정책의 보존 기간
7. 7.1의 이름·표기 통일안
8. 조회 REST의 포트·인증(내부망 토큰?)·버전 접두사(`/v1`)
9. `request.cancelled` 형식과 "생성 중 취소"의 의미 (진행 중 결과를 어디까지 버릴지)
10. 세그먼트 메타 재조회를 엔진이 제공할지, 백엔드가 자기 큐에 보관할지
11. 곡 음원의 공유 폴더 경로 규칙과 카탈로그 라이선스 필드
