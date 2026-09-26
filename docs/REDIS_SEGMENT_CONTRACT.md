# Engine ↔ Backend Redis 전달 계약 v1 — 협의 초안

상태: **과거 협의 초안 — 현재 구현 규격은 docs/ENGINE_REDIS_CONTRACT.md 우선**

현재 백엔드는 실제 엔진의 중괄호 없는 키(engine:st_local_dev:out), type=segment.submitted,
여러 문자열 envelope 필드와 payload JSON을 사용한다. 아래 data 단일 필드 형식과
중괄호 포함 키 제안은 채택하지 않았다. 구현 상태는 apps/backend/README.md를 참고한다.
작성일: 2026-09-16. 기준: PR #20 병합 main의 엔진 코드 및 현재 Redis 연결 작업.
이 문서는 코드 변경 지시나 합의 완료 기록이 아니다. 아래 체크리스트를 양쪽 담당자가 확인한 뒤 구현한다.

## 1. 이번에 합의할 범위

첫 연결 목표: 엔진이 완성한 WAV 1개와 metadata를 발행하고, 백엔드가 검증하여 대기 큐에 한 번 반영한다.
Redis에는 metadata만 저장하고 오디오 binary는 공유 폴더에 둔다.
엔진의 생성 세그먼트는 길이가 가변적이며, 후속 FFmpeg의 약 4초 HLS 조각과는 다른 단위다.
이번 단계에 HLS 재생, 방송 시계, 사용자 요청 왕복, 상태 머신 전체 구현은 포함하지 않는다.

## 2. 실제 코드와 제안의 차이

| 항목 | 현재 코드 | v1 제안 |
| --- | --- | --- |
| 전송 | StdoutTransport, RedisTransport 미구현 | 기존 publish_segment에 Redis Streams 어댑터 연결 |
| ID | seg_ + UUID hex 12자리 (ULID 아님) | 문자열 ID 유지, 재시도에도 같은 ID |
| created_at | time.time() 숫자, Unix 초 | 숫자 유지; ISO 문자열로 바꾸지 않음 |
| 버전 | 없음 | envelope에 contract_version=1 추가 |
| audio_ref | 생성 경로의 str 표현 | 공통 오디오 루트 기준 상대 경로, 구분자는 / |
| 오디오 | DummyTts: 16 kHz, mono, 16-bit PCM WAV | 초기 테스트는 해당 포맷; 실제 TTS 포맷 변경은 별도 합의 |
| priority | ack=10, request_reply=30, 기본=50 | 작은 수 우선 의미 제안; 첫 수신 테스트에서는 FIFO |

참고 코드: apps/engine/src/onair_engine/domain.py, transport.py, engine.py,
pipeline/pipeline.py, pipeline/tts.py, ack.py, scheduler/scheduler.py.
기존 docs/ENGINE_ARCHITECTURE.md 5장의 ISO 시각·ULID 예시보다 실제 코드를 기준으로 잡았다.

## 3. Redis 키와 실행 환경

아래 키의 중괄호는 Redis 키에 실제로 남긴다. 예: engine:{st_local_dev}:out.
station_id 허용 문자 제안: ASCII 영문/숫자/밑줄/하이픈, 길이 1~64.

| 키 | 쓰는 쪽 → 읽는 쪽 | 용도 |
| --- | --- | --- |
| engine:{st_local_dev}:out | Engine → Backend | 이번 단계 segment.generated |
| engine:{st_local_dev}:in | Backend → Engine | 후속 이벤트용 예약, 현재 구현하지 않음 |
| engine:{st_local_dev}:out:dead | Backend → 운영 확인 | 처리 불가능 메시지 격리 |

in/out은 **엔진 기준**이다. 스테이션별 다른 키를 사용한다.
초기 개발은 하나의 PC에서 엔진과 백엔드를 실행하고 Redis만 Docker로 실행한다.
양쪽 REDIS_URL 기본값: redis://127.0.0.1:6379/0.
양쪽을 Docker에 넣는다면 127.0.0.1 의미가 달라지므로 별도 연결 설정이 필요하다.
각자 다른 PC에서 같은 경로 이름을 지정하는 것은 공유 저장소가 아니다.
원격 개발 시 실제 공통 마운트나 파일 전달 방식은 따로 합의한다. 현재 Compose는 로컬 접속 전용이다.

## 4. 메시지 형식 제안

Stream entry는 `data`라는 필드 하나에 UTF-8 JSON 문자열을 넣는다.
아래는 data 필드를 JSON으로 디코딩한 예시다. Redis가 만드는 entry ID는 payload.id와 별개다.

```json
{
  "contract_version": 1,
  "event_type": "segment.generated",
  "payload": {
    "id": "seg_123456789abc",
    "station_id": "st_local_dev",
    "audio_ref": "st_local_dev/seg_123456789abc.wav",
    "duration_ms": 4590,
    "corner_type": "opening",
    "kind": "speech",
    "priority": 50,
    "reorderable": true,
    "request_ref": null,
    "music_ref": null,
    "created_at": 1789516800.125
  }
}
```

| payload 필드 | 타입 / v1 검증 제안 |
| --- | --- |
| id | 비어 있지 않은 문자열, 최대 128자; (station_id, id)가 중복 판정 키 |
| station_id | 스트림 키의 station_id와 정확히 일치 |
| audio_ref | 루트 기준 POSIX 상대 경로, 아래 파일 규칙 적용 |
| duration_ms | 양의 정수, bool 제외; WAV 실측값과 오차 1ms 이내 |
| corner_type | 비어 있지 않은 문자열, 최대 64자; 현재 코너 추가를 막는 enum은 두지 않음 |
| kind | speech / music / ack / filler |
| priority | 0~100 정수, bool 제외 |
| reorderable | JSON boolean |
| request_ref, music_ref | 문자열 또는 null; v1 발행 시 두 필드 모두 명시 |
| created_at | 유한한 0 이상 JSON number, Unix 초; bool·ISO 문자열·NaN·Infinity 거부 |

표의 필드는 모두 필수로 제안한다. 알 수 없는 필드/버전/이벤트는 v1에서 격리하고 자동 해석하지 않는다.
JSON encoded 메시지는 최대 64 KiB로 제안한다. WAV 크기 상한은 실제 TTS/음원 요구 확인 후 확정한다.
created_at은 생성 기록용이며 방송 시작 시간이나 재생 순서를 뜻하지 않는다.
버전은 외부 envelope에 있으므로 엔진 내부 SegmentSubmission에 즉시 필드를 추가할 필요는 없다.

직렬화 예시(설명용, 실행 코드 아님):

```python
await redis.xadd(stream_key, {
    "data": json.dumps(envelope, ensure_ascii=False, allow_nan=False)
})
```

## 5. 오디오 파일 규칙

- 공통 설정 이름 제안: SHARED_AUDIO_DIR. 양쪽에서 동일한 실제 폴더를 바라보는 절대 경로로 설정.
- 기존 엔진 transport.audio_dir와 공통 설정의 관계: SHARED_AUDIO_DIR가 있으면 우선,
  없으면 기존 설정을 엔진에서 절대 경로로 해석. 백엔드에도 같은 실제 루트를 지정한다.
- 예: 루트가 C:/onair-data/audio이면 audio_ref는 st_local_dev/seg_123456789abc.wav.
- 엔진은 파일 쓰기와 close를 완료한 뒤 발행한다. 임시 파일을 같은 디렉터리에 쓴 후
  최종 이름으로 원자적 rename하는 방식 제안. 발행 후 파일 내용을 덮어쓰지 않는다.
- 백엔드는 절대 경로, 드라이브/UNC 경로, 역슬래시, 빈 경로 요소, . 및 .. 요소를 거부한다.
  resolve 후 실제 파일이 공유 루트와 해당 station 디렉터리 내부에 있는지도 확인한다.
- 확장자만 보지 않고 WAV header와 프레임 수로 형식·길이를 검증한다.
- ack 캐시는 여러 세그먼트가 같은 WAV를 참조할 수 있다. 파일명과 segment ID가 같다고 가정하지 않는다.
  캐시도 게시 후 불변이어야 하므로 재기동 시 기존 ack_0.wav를 덮어쓰기보다 버전별 경로를 제안한다.
- 첫 테스트에서는 자동 파일 삭제를 하지 않는다. 후속 삭제는 백엔드의 재생 완료·참조 해제 후 정한다.

## 6. 발행·수신·중복 처리

### 엔진

1. 오디오 파일 완성 → SegmentSubmission 구성 → 경로 정규화 → envelope 직렬화 → XADD.
2. publish_segment 반환은 Redis가 메시지를 받아들였다는 뜻이다. 백엔드 수락/재생 완료가 아니다.
3. 연결 실패 또는 응답 유실 시 같은 ID, 시각, 내용으로 재시도한다. 새 ID를 발급하지 않는다.
4. 제안 재시도: 0.5/1/2초 간격으로 3회, 이후 명시적 실패 처리 및 미발행 자료 보존.
   프로세스 재시작 후 재발행이 필요하면 outbox(미발행 기록)의 저장·복구도 구현해야 한다.
5. 현재 Scheduler._publish는 발행 전에 produced_ms/submitted를 증가시킨다.
   Redis 발행 실패를 성공으로 세지 않도록 성공 후 증가로 변경하고 task 예외도 회수해야 한다.
   이 카운터는 엔진 근사치이며 백엔드의 실제 재생 가능 버퍼를 대체하지 않는다.

### 백엔드

- consumer group 제안: backend-segments-v1, consumer 이름은 실행 인스턴스마다 고유하게 생성.
- 최초 그룹은 시작 ID 0-0으로 생성해 먼저 도착한 항목도 읽는다. 기존 그룹은 삭제/초기화하지 않는다.
- XREADGROUP으로 읽고 파일/metadata 검증 후 Redis에 segment 기록, 대기 큐, 중복 방지 기록을 반영한다.
- 동일 (station_id, id)와 동일 payload는 이미 반영된 것으로 처리한다. 다른 payload면 충돌로 격리한다.
- **큐 반영·중복 방지 기록을 원자적으로 저장한 뒤 XACK**한다.
  메모리 큐에만 넣고 XACK하면 재기동 시 사라지므로 허용하지 않는다.
  저장 직후 XACK 전 실패한 경우에는 재처리 시 중복 기록을 확인하고 XACK만 수행한다.
- XACK는 메시지 처리 확인이며 PLAYED 상태도, 음성 접수 안내(kind=ack)도 아니다.
- pending(읽었지만 확인되지 않은 항목)은 재기동 후 XAUTOCLAIM 등으로 회수한다.
  제안: 수신 처리를 5초 이내로 제한, 30초 이상 유휴 항목 회수, 시도 횟수는 Redis에 기록.
- 파일 누락/일시적 접근 실패는 최초 처리 포함 최대 3회 시도 후 격리한다.
  잘못된 스키마·루트 밖 경로·ID 충돌·미지원 버전/이벤트는 즉시 격리한다.
- 격리 스트림에 원본 entry ID, 원문, 사유를 성공적으로 기록한 뒤 원본 XACK.
  격리 기록의 중복 방지도 포함하며 기록 실패 시 XACK하지 않는다.
- Redis 자체 연결 실패는 내용 오류와 구분해 재접속한다. 소비자에게 재전송 가능성이 있으므로
  정확히 한 번의 전달이나 재생을 보장한다고 표현하지 않는다.

초기에는 스테이션 1개, 명시적 consumer 1개로 실행한다. API worker마다 소비자를 기동하지 않는다.
Streams 수신 순서와 최종 방송 편성 순서는 구분한다. 생성 병렬 처리로 완료 순서가 바뀔 수 있다.
첫 테스트는 수신 순서 FIFO이며 priority/reorderable은 저장만 한다.
실제 편성 순서 보장이 필요하면 engine sequence/order_revision 및 재배치 규칙을 다음 계약에서 정한다.

Redis 영속성·보존: 현재 Compose AOF와 볼륨을 사용하나 장애 시 완전 무손실 보장은 아니다.
첫 테스트는 자동 XTRIM/TTL 없이 진행하며 대량 발행은 제한한다.
운영 전 미처리 항목 보존, 중복 기록 수명, dead stream 보존과 용량 제한을 함께 정해야 한다.

## 7. 후속 이벤트와 책임 경계

엔진: 생성/편성 결정, 생성 전후 요청 상태. 백엔드: 입력 검증, 방송 대기 큐, 믹싱/게시/재생 상태.
현재 Node에서 이식한 backend scheduler는 프로토타입이며 엔진에 생성 명령을 보내지 않는다.
엔진의 request.state 통보와 events 소비는 다음 계약으로 분리한다.
단, 현재 생성 흐름에서 notify_request_state도 호출하므로 첫 segment-only Redis 어댑터에서는
이 메서드는 명시적으로 stdout 진단만 유지하고, events는 미연결로 둔다는 데 합의가 필요하다.

향후 request.arrived 구현 전에는 백엔드 UUID id/prompt/listenerId와 엔진
request_id/body/requester_ref/kind를 매핑하고 ID 재발급 여부를 정해야 한다.
현재 API 요청이 엔진에 전달되거나 상태가 자동 동기화된다고 가정하면 안 된다.

## 8. 담당자별 작업과 합의 체크리스트

| 담당 | 구현 범위 |
| --- | --- |
| 엔진 | RedisTransport, 설정 주입과 연결 종료, 경로 변환, 파일 불변성, 발행 재시도/카운터·task 오류 처리 |
| 백엔드 | 수신 모델, consumer group, 파일 검증, 원자적 큐·중복 저장, pending 회수, 격리 |
| 공동 | 예시 payload 고정, 단일 공유 폴더, 실제 WAV 관통·중복·재시작 테스트 |

- [ ] 단일 PC + 공유 폴더를 첫 통합 테스트 환경으로 사용한다.
- [ ] engine:{station_id}:out + data JSON envelope(contract_version=1)에 동의한다.
- [ ] 현재 ID/Unix 초를 유지하고 필드·제한·WAV 형식에 동의한다.
- [ ] 공유 루트·상대 경로·ack 캐시 불변성에 동의한다.
- [ ] 발행 재시도 시 같은 ID/내용을 유지한다.
- [ ] 영속 큐/중복 저장 후 XACK, 재처리/격리 기준에 동의한다.
- [ ] 처음에는 FIFO이며 편성 순서/우선순위 재배치는 후속으로 진행한다.
- [ ] request.state/events는 후속이며 이번 어댑터의 미지원 동작을 명시한다.

합의자/날짜: 엔진 ______ / 백엔드 ______ / 날짜 ______
미합의 항목 및 수정안: ______

## 9. 연결 완료 기준 (아직 미실행)

1. 더미 TTS로 WAV 1개 생성, 실제 Redis entry에서 JSON 확인.
2. 백엔드 수신 후 큐 1건, duration_ms 1회 반영, XACK 확인.
3. 같은 ID/내용을 재발행해도 큐/버퍼가 증가하지 않음; 다른 내용은 격리.
4. 백엔드 중지 중 발행 → 재기동 후 수신; 수신 후 XACK 전 강제 종료 → 중복 없이 복구.
5. 누락/손상 파일, 경로 탈출, 미지원 버전에서 정해진 재시도/격리 동작 확인.
6. Redis 중지 중 엔진 발행 실패 → 성공 카운터 미증가, 복구 후 같은 ID 재발행.
7. 서로 다른 방송 데이터 격리 및 동일 ack 파일 재사용 확인.

## 10. 엔진 담당자에게 공유할 짧은 메시지

> FastAPI 전환과 Redis PING 확인까지 완료했습니다. 다음 단계의 세그먼트 전달 계약을 맞추려 합니다.
> Redis Streams의 engine:{station_id}:out에 data JSON으로 버전·이벤트 타입·기존 metadata를 보내고,
> 오디오는 공유 폴더에 완성한 뒤 상대 경로로 전달하는 안입니다.
> created_at은 현재 코드의 Unix 초를 유지하고 재전송 시 같은 segment ID를 사용하려 합니다.
> 초기에는 WAV 1개를 수신·검증·중복 없이 큐에 넣는 데 집중하고 요청 이벤트와 HLS는 후속으로 두려 합니다.
> 8번 체크리스트와 경로/ACK 처리안을 검토하고 수정이 필요한 항목을 알려주세요.

## 공식 참고

- [XREADGROUP](https://redis.io/docs/latest/commands/xreadgroup/): 소비 그룹으로 수신
- [XACK](https://redis.io/docs/latest/commands/xack/): pending 처리 확인; 메시지 삭제 명령 아님
- [XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/): 유휴 pending 소유권 회수

구현은 현재 Redis 7.4에서 지원하는 명령 옵션에 맞춘다.
