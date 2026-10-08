# 백엔드 방송 처리 및 복구

## 범위

FastAPI → Redis 요청 접수 → 엔진 이벤트 수신 → FFmpeg 인코딩 → HLS 발행을 연결한다.
엔진·프론트엔드 코드는 변경하지 않는다. 한 스테이션당 백엔드 프로세스 한 개로 실행한다.

## 요청 상태와 방송 클럭

정상 상태는 `requested → screened → queued → generating → generated → mixing → published → played`이다.
엔진이 전달하지 않은 중간 상태는 임의로 만들지 않는다. 생성 재시도의 `generating → queued`는 허용한다.
거절은 `rejected`, 백엔드 음원 처리 실패는 `failed`로 종료한다. `failed`는 백엔드 조회용 확장이다.
방송 처리가 시작된 후 늦게 도착한 엔진 상태는 현재 상태를 되돌리지 않고 이력에 `accepted: false`로 기록한다.

- MIXING: 음원 검증·인코딩 시작.
- PUBLISHED: 해당 음원의 모든 HLS 조각을 재생목록에 발행.
- PLAYED: 서버 방송 시간표상 마지막 조각의 재생 종료. 청취자별 실제 청취 완료가 아니다.
- `ONAIR_BROADCAST_DELAY_SECONDS` 기본 12초를 발행 시각에 더해 방송 시작 시각을 계산한다.
  HLS의 `EXT-X-PROGRAM-DATE-TIME`과 조각 metadata의 `broadcastStartAt`으로 노출한다.
  실제 브라우저의 버퍼·접속 시각에 따른 오차는 별도 측정 대상이다.
- 요청 완료는 현재 엔진의 요청당 단일 `speech / request_reply` 음원을 기준으로 한다.
  짧은 접수 안내 `ack`는 요청 완료로 처리하지 않는다. 여러 응답 음원을 묶는 규칙은 별도 합의가 필요하다.

## 조회 API

| 경로 | 의미 |
| --- | --- |
| GET /api/requests/{request_id}/state | Redis에 저장된 최신 요청 상태 |
| GET /api/requests/{request_id}/history?offset=0&limit=100 | 접수·엔진·방송 상태 이력. limit 최대 200, 없는 요청은 빈 목록 |
| GET /api/broadcast/segments/{segment_id}/state | 세그먼트 방송 상태. 없으면 404 |
| GET /api/stream/state?fragment={TS 파일명} | 해당 조각의 metadata와 방송 운영 상태 |

Redis 조회 실패는 요청·세그먼트 API에서 503으로 응답한다. HLS 재생과 상태 조회 가능 여부는 별개다.
실시간 상태 전달은 조회 방식이며 WebSocket push는 추가하지 않았다.

## 버퍼와 폴백

- `bufferSeconds`: 인코딩 완료한 원본 음원의 미발행 분량 + 서버 시간표상 남은 원본 재생시간.
  폴백 음악은 제외한다.
- `queuedAudioSeconds`: 아직 처리하지 않은 큐의 메타데이터 기반 예상 길이. Redis 연결 불가 시 null.
- `totalAudioEstimateSeconds`: 위 두 값의 합. 큐를 조회할 수 없으면 null.
- `bufferLow`: 실제 준비된 원본 버퍼가 `ONAIR_BUFFER_LOW_SECONDS`(기본 15초)보다 적음.
- `fallbackActive`: 발행기가 현재 폴백을 발행 중인지 표시한다. 청취자 재생 상태는 fragment 조회를 사용한다.
- `queueAvailable`, `pendingStateEvents`: 큐 조회 가능 여부와 미전송 상태 이벤트 수.

원본을 준비하는 동안 기본 폴백은 자체 생성한 짧은 멜로디다. `ONAIR_FALLBACK_AUDIO`로 로컬 파일을 지정하거나
`ONAIR_FALLBACK_MODE=silence`로 무음을 선택할 수 있다. 폴백 음악 변환 실패 시 무음으로 대체한다.
원본이 준비되면 현재 폴백 묶음 이후 이어서 발행한다. 조각 metadata 상태는 `segment / fallback / silence / unavailable`이다.
`backpressure` 이벤트는 임계 상태 변경 또는 약 10초마다 엔진 입력 스트림에 전달한다.

## 장애와 재시작

스테이션 HLS 폴더의 `broadcast.sqlite`에 작업 상태와 전송 대기 이벤트를 저장한다.
Redis 장애 중에도 이미 준비한 오디오와 폴백을 발행하며, 연결 회복 후 이벤트를 재전송한다.
같은 이벤트 ID의 재전송은 Redis에서 중복 반영하지 않는다. FFmpeg 인코딩 시간도 SQLite measurements에 기록한다.
이는 전체 프롬프트→생성→청취 지연을 측정한 값은 아니다.

재시작하면 MIXING/PUBLISHED 상태의 원본을 처음부터 다시 처리한다. PLAYED/FAILED 음원은 재생 대상에서 제외한다.
따라서 재시작 시 일부 음원이 반복될 수 있고 브라우저 재연결도 필요할 수 있다. 완전한 무중단 재시작이나 정확히 한 번 청취를 보장하지 않는다.
Redis 중단 중 접수 API는 503을 반환하므로 클라이언트는 동일 Idempotency-Key로 재시도한다.

동일 HLS 폴더에 두 발행기가 접근하면 파일 잠금으로 두 번째 실행을 거부한다.
서로 다른 HLS 폴더·호스트까지 막는 분산 잠금은 아니므로 `--workers 1`과 단일 실행을 유지한다.
Redis 데이터와 HLS 폴더는 함께 보존해야 복구할 수 있다.

## 남은 협업 및 운영 과제

- 엔진: 초기 QUEUED 이벤트 발행, state.transition·backpressure 수신 후 실제 편성 반영.
  현재 엔진은 이 두 종류를 수신해도 편성에 적용하지 않는다.
- 프론트엔드: 요청 상태·이력, null 통계, fragment별 표시와 방송 클럭을 연동한다.
- 인증·본인 확인, 요청 목록 페이지네이션, Redis 이력·중복방지 키·SQLite의 보관 기간 및 용량 제한은 미구현이다.
- 폴백의 청취 품질과 실제 브라우저 연속 재생은 별도 청취 검증이 필요하다.
