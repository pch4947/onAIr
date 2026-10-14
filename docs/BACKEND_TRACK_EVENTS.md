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

소개 연동 모드의 `backpressure.d_total_ms`에는 아직 소개·예약되지 않은 음악 후보를
넣지 않습니다. 후보 곡 길이가 목표 버퍼보다 길면 엔진이 소개 생성을 멈출 수 있기 때문입니다.
소개가 접수되어 예약된 곡, 인코딩 중인 곡, 방송 시간표에 발행된 곡은 버퍼에 포함합니다.
직접 송출 모드(0)의 버퍼 계산은 유지합니다.

## 음원 등록 API

기존 `POST /api/stations/{station_id}/tracks`에 선택 필드 `source`(기본 playlist),
`request_id`를 추가합니다. 로그인 인증과 Idempotency-Key 요구사항은 유지합니다.
같은 키로 다른 곡·source·request_id를 보내면 409입니다. Swagger는 FastAPI 모델에서 생성됩니다.
`request`·`vote`는 출처 메타데이터이며 청취자 채팅에서 자동 선곡하는 기능은 아닙니다.

## 검증과 다른 파트 작업

`python -m unittest discover -s tests -p test_track_events.py -v`로 서버 시각과 소개 대기를 검증합니다.
Redis 통합 검증은 `ONAIR_INTEGRATION_TEST=1`로 실행합니다. UUID로 분리한 테스트 키만 정리합니다.

실제 엔진 M3까지 포함한 검증은 백엔드 폴더에서 다음과 같이 실행합니다.

```powershell
$env:ONAIR_INTEGRATION_TEST = '1'
$env:ONAIR_FFMPEG = (Get-Command ffmpeg).Source
.venv/Scripts/python.exe -m unittest discover -s tests -p test_music_intro_e2e.py -v
```

선택적으로 `ONAIR_E2E_AUDIO_FILE`에 이미 다운로드한 실제 음원 경로를 지정할 수 있습니다.
없으면 테스트 톤을 생성합니다. 곡이 목표 버퍼·보고 주기보다 길도록 15초 이상으로 준비하고,
실제 엔진이 생성한 소개→예약 곡→소개 제출 이후 도착한 신청곡의 순서를 확인합니다.
Redis DB15의 UUID 테스트 방송만 사용하며 자기 키와 control 항목만 정리합니다.
LLM/TTS는 dummy이므로 실제 DJ 음성 품질은 별도 검증해야 합니다.
테스트는 서버 서비스를 재시작하거나 운영 모드를 활성화하지 않습니다.

엔진 M3의 곡 이벤트 수신과 소개 생성은 위 통합 테스트에 포함합니다.
실제 LLM/TTS를 사용하는 방송 품질, HELD 등 후속 입력 정책과 프론트 음악 큐 표시는
별도 검증·작업 범위입니다. 운영 환경의 소개 연동 모드는 별도 배포 검증 후 활성화합니다.
