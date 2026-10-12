# 백엔드 곡 이벤트 (#104)

## 계약과 시각

음원 등록 API는 다운로드와 검증이 끝난 곡만 `track.queued`로 엔진에 알립니다.
`engine:{station_id}:in`에 v1 envelope(type, event_id, station_id, at, payload)를 사용합니다.

- `track.queued`: `{track: {track_ref, title, artist, duration_ms}, source, request_id?}`.
  source는 playlist/request/vote입니다. 첫 등록과 다음 곡 결정 시 한 곡을 알립니다.
  소개 제출 전의 request/vote 등록은 기존 다음 곡을 교체하며, 새 이벤트가 이전 이벤트를 대체합니다.
- `track.started`: `{track_ref, started_at}`. 첫 HLS 조각 공개 시각에
  `ONAIR_BROADCAST_DELAY_SECONDS`를 더한 서버 방송 시각입니다.
  해당 시각이 되면 내구성 outbox에서 발행합니다. 청취자 기기의 실제 재생 시각이 아닙니다.
  곡 세그먼트별 시작 기록과 이벤트 ID로 재시도·재시작 시 중복을 막습니다.
- `PLAYED`는 기존대로 서버 방송상 재생 완료입니다.

## 소개 멘트 연동 모드

`ONAIR_TRACK_INTRO_ENABLED=0`이 기본값입니다. 기존 직접 음악 큐 재생을 유지합니다.
엔진이 위 이벤트를 처리하고 실제 예약 곡의 `music_ref`가 있는 소개를 제출하도록 배포된 뒤
1로 변경합니다. 구형 엔진에서 켜면 곡은 소개를 기다리고 폴백이 송출됩니다.

모드 1에서는 speech/glue 소개 접수와 곡 예약을 Redis에서 원자적으로 처리합니다.
예약 이후 신청·투표로 들어온 곡은 그다음으로 미루고, 현재 소개 바로 다음에는 예약 곡을 인코딩합니다.
없는 곡이나 중복 소개는 격리합니다. 인코딩 실패 시 예약을 해제합니다.
곡 시작 시 다음 대기 곡을 알리며, 대기 곡이 없으면 등록된 playlist 곡을 순환합니다.
다음 곡을 다운로드할 외부 카탈로그 자체는 이 변경에 포함되지 않습니다.

## 음원 등록 API

기존 `POST /api/stations/{station_id}/tracks`에 선택 필드 `source`(기본 playlist),
`request_id`를 추가합니다. 소유자 인증과 Idempotency-Key 요구사항은 유지합니다.
같은 키로 다른 곡·source·request_id를 보내면 409입니다. Swagger는 FastAPI 모델에서 생성됩니다.
`request`·`vote`는 출처 메타데이터이며 청취자 채팅에서 자동 선곡하는 기능은 아닙니다.

## 검증과 다른 파트 작업

`python -m unittest discover -s tests -p test_track_events.py -v`로 서버 시각과 소개 대기를 검증합니다.
Redis 통합 검증은 `ONAIR_INTEGRATION_TEST=1`로 실행합니다. UUID로 분리한 테스트 키만 정리합니다.

엔진의 이벤트 수신·소개 생성·HELD 처리와 프론트의 화면 연동은 별도 팀원 작업입니다.
따라서 이 PR은 #104의 백엔드 부분만 완료하며 전체 이슈를 닫지 않습니다.
