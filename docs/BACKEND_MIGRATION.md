# Backend 단계별 전환 계획

## 확인한 기준

- main: 629b7123867ca9801bee06b01726a902fdd1c595 (PR #14 revert merge)
- Node: 직접 작성한 HTTP router, 메모리 요청 배열/방송 상태, 버퍼 기반 정책
- Engine: Python >=3.12, pyproject.toml, StdoutTransport 기본 / RedisTransport 미구현
- Frontend: React/TypeScript, mock WAV 재생. hls.js는 의존성만 존재
- packages: 공유 코드 없이 README만 존재

## 유지 / 교체

| 대상 | 방향 |
| --- | --- |
| /health 응답 | 그대로 유지 |
| 청취자 요청과 방송 상태 개념 | 유지, 후속 단계에서 station_id 기준으로 설계 |
| 버퍼량·생성 지연·요청 대기시간 | 정책 입력 개념 유지 |
| Node HTTP 서버와 수동 JSON 처리 | FastAPI/Uvicorn 및 요청 모델로 교체 |
| 수동 .env 파서 | python-dotenv로 교체 |
| Node scheduler | 기존 정책을 Python으로 이식, 엔진과 책임 분리는 Redis 단계에서 정리 |
| 프로세스 메모리 상태 | 개발 프로토타입 한정, Redis 상태 및 방송별 소유권으로 확장 |

첫 커밋 범위인 기존 API Python 이식과 Node 코드 제거를 완료했다. 정상 응답은 유지하고 잘못된 입력은 FastAPI 422로 처리한다.
Node 요청 큐에는 소비/완료 처리가 없으므로 그대로 최종 구조로 옮기지 않는다.
엔진은 생성/편성을 담당하고 백엔드는 수신 검증·방송 큐·확정된 오디오 재생을 담당하는 방향이다.

## 단계 및 완료 조건

1. FastAPI 최소 서버: 실제 HTTP /health 200, 기존 응답, /docs 및 OpenAPI 확인.
2. 기존 요청/상태 API 계약 정리 및 필요한 동작 이식: 회귀 검증 후 Node 파일 제거.
3. Redis 개발 환경/연결: 수명주기로 연결/종료하고 ping 및 연결 실패 확인.
4. Engine publish: 파일 쓰기 완료 후 metadata 발행, Redis에서 메시지 확인.
5. Backend consume: 검증·중복 방지·재시도·확인 처리와 재시작 시 복구 확인.
6. 방송별 queue/buffer: 대기 오디오와 committed 오디오 중복 합산 방지.
7. FFmpeg/HLS: 하나의 방송별 출력 파이프라인으로 연속 오디오, 약 4초 AAC/TS 생성.
8. HTTP HLS 제공 및 브라우저 재생: playlist/segment GET, 늦은 접속의 live edge 합류 확인.
9. WebSocket metadata 및 broadcast clock 동기화.

## Redis 전달 계약 제안 (미구현)

기존 엔진 문서 6장의 Redis Streams + 공유 오디오 저장소 방향을 따른다.
Pub/Sub 대신 소비 확인과 재처리를 설계할 수 있는 Streams를 후보로 둔다.
메시지에는 audio_ref와 metadata만 담고 WAV binary는 넣지 않는다.

현재 SegmentSubmission에는 contract_version이 없고 created_at은 Unix timestamp(float)다.
붙여넣은 과거 PR 예제를 그대로 스키마로 쓰지 않는다. 변경 시 엔진과 함께 계약을 맞춘다.
audio_ref는 현재 생성 경로 문자열이므로 공유 저장소 루트 기준 상대 경로로 정규화해야 한다.
수신 측에서는 루트 밖 경로 접근 방지와 실제 파일 검증이 필요하다.

`engine:{station_id}:in` / `:out` 방향은 엔진 기준으로 명시하고 이벤트 envelope를 합의한다.
Streams 자체가 exactly-once 재생을 보장하지 않는다. 소비 그룹, pending 회수, 세그먼트 ID 중복 방지,
ACK 시점, Redis 영속성/보존 정책을 함께 설계해야 한다.
HTTP worker마다 consumer/FFmpeg를 기동하면 중복 방송이 되므로 방송별 단일 소유자가 필요하다.

## 권장 폴더 확장

현재 app/main.py, config.py, __main__.py와 API 모듈 api.py, 정책 모듈 scheduler.py를 사용한다.
기능이 커지면 API와 도메인 모듈을 하위 패키지로 분리하고,
Redis 단계에서 app/messaging/, HLS 단계에서 app/streaming/을 추가한다.
존재하지 않는 기능을 위한 빈 디렉터리는 미리 만들지 않는다.

## 작업 이슈

- 이슈: [#18](https://github.com/pch4947/onAIr/issues/18)
- 범위: Node.js 백엔드를 Python FastAPI로 전환
- 브랜치: feat/#18-fastapi-migration

## 참고

- https://fastapi.tiangolo.com/tutorial/first-steps/
- 기존 docs/ENGINE_ARCHITECTURE.md 6장

## 현재 구현 상태

app/api.py와 app/scheduler.py에 기존 API 및 정책을 이식했다. Node 파일은 제거했다.
첫 커밋은 FastAPI 전환, 두 번째 커밋은 Redis 연동으로 묶는다. Redis와 HLS는 아직 미구현이다.
