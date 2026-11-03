# 다중 방송 (백엔드 설계 초안)

> 상태: **초안 — 팀 합의 전**. `[결정 필요]` 항목은 이슈에서 정한 뒤 이 문서를 고친다.
> 구현은 #157 3단계(계약 v2 전환) 이후에 시작한다. 같은 파일(`requests.py`, `consumer.py`,
> `journal.py`, `stations.py`)을 크게 바꾸므로 동시에 진행하면 충돌이 많다.

## 1. 목표와 범위

여러 호스트가 동시에 각자 방송을 열고, 청취자는 진행 중인 방송 중 하나를 골라 듣는다.

| 정한 것 | 내용 |
| --- | --- |
| 동시 방송 제한 | 사용자당 진행 중 방송 1개 + 서버 전체 상한 (설정값) |
| 기존 API | 유지한다. 방송 ID가 없는 경로는 **기본 방송**으로 동작하고, 방송별 경로를 새로 추가한다 |
| 실행 구조 | 백엔드 프로세스 하나 안에서 방송별 런타임을 띄운다. 배포·systemd 구성은 그대로 |

포함하지 않는 것: 방송별 프로세스 분리, 여러 서버로의 수평 확장, 방송 중 상한 변경,
프론트엔드 화면(목록·선택) 구현 — 프론트 작업은 6절에 필요한 API만 정리한다.

엔진은 이미 `engine:control`의 `station.created`마다 방을 하나씩 띄운다
([ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) M3). 계약(`engine:{sid}:in`/`out`)이 방송별
스트림이므로 **엔진 변경은 없다**.

## 2. 현재 구조에서 단일 방송에 묶인 곳

| 위치 | 단일 방송 전제 |
| --- | --- |
| `config.py` | `ONAIR_STATION_ID` 하나를 시작할 때 정한다 |
| `redis_connection.py` | `HlsPublisher`, `EngineConsumer`, `StationLifecycle`, `RequestStore`, `StationStore`, `ReplaySessionStore`, `PresenceStore`, `MusicService`, 다시듣기 북마크가 `app.state`에 하나씩 |
| `stations.py` | 생성 Lua가 키 하나(`backend:{sid}:station`)만 본다. 조회·정지는 다른 ID를 404 |
| `api.py` | `/api/requests`, `/api/stream/state`, `/api/broadcast/queue`, `/api/requests/{id}/state·history`, `/api/broadcast/segments/{id}/state`, `/api/scheduler/tick`에 방송 ID가 없다 |
| `music.py`, `replays.py`, presence, `/hls/...` | 경로에 `{station_id}`가 있지만 설정값과 다르면 404 |
| `replays.py` `/replays/mine` | 소유자 판단을 현재 방송 레코드 하나로 한다 |
| 디스크 | HLS `hls_dir/{sid}`, 음원 `audio_dir/{sid}` — 이미 방송별 디렉터리 |
| 프론트 | `/listen`에 방송 ID가 없고 `/api/stream/state`의 `stationId`를 받아 쓴다 |

방송별 Redis 키(`backend:{sid}:*`)와 디스크 경로는 이미 방송 ID로 나뉘어 있다.
바꿀 것은 주로 **"하나만 만든다"는 연결부와 라우팅**이다.

## 3. 방송 ID 방식 `[결정 필요]`

| | A. 고정 채널 (권장) | B. 방송마다 새 ID |
| --- | --- | --- |
| ID | 설정한 채널 목록 `ONAIR_STATION_IDS=st_ch1,st_ch2,st_ch3`. 빈 채널에 새 방송을 연다 | 생성할 때마다 `st_{랜덤}` 발급 |
| 상한 | 채널 수 = 상한 | 별도 설정값 |
| 재사용 | 지금처럼 같은 ID에 `generation`으로 새 방송 — 이미 구현·검증된 종료 정리, 엔진 스트림 보관, 이전 방송 이벤트 무시 로직을 그대로 쓴다 | 재사용이 없어 세대 구분이 필요 없지만, 런타임을 방송마다 만들고 지워야 하고 재시작 때 진행 중 방송을 찾아 복구해야 한다 |
| 런타임 | 시작할 때 채널마다 만든다. 발행기는 방송이 있을 때만 돈다(지금도 상태에 따라 start/stop) | 생성·종료 때 동적으로 만들고 지움 |
| 다시듣기 | 채널별 목록. "내 다시듣기"·북마크는 채널을 모아 응답 | 방송별로 흩어져 전역 색인이 필요 |
| 누적 | 채널 수만큼만 키·디렉터리 | 방송마다 키·디렉터리가 남아 보존 정책 필요 |
| 기존 데이터 | 기존 `st_local_dev`를 첫 채널로 두면 이전 없음 | 기존 방송을 새 구조로 옮길지 정해야 함 |

권장: **A**. 백엔드 변경을 연결부와 라우팅에 묶어 두고, 가장 복잡한 종료 정리·세대 구분 코드를
건드리지 않는다. 청취자 화면에는 채널 번호가 아니라 방송 정보(DJ 이름, 주제)를 보여 주면 된다.
아래 절은 A 기준이다.

## 4. 런타임 구조

```
app.state.stations_runtime: dict[station_id, StationRuntime]
app.state.default_station_id: 첫 번째 채널 (기존 경로가 쓰는 방송)

StationRuntime
  settings      방송 ID가 바뀐 Settings 사본 (dataclasses.replace)
  stations      StationStore
  requests      RequestStore
  presence      PresenceStore
  replays       ReplaySessionStore + 북마크 LikeStore
  music         MusicService
  consumer      EngineConsumer        — 방송별 task 1개 (engine:{sid}:out)
  hls           HlsPublisher          — 방송이 started일 때만 실행
  lifecycle     StationLifecycle      — 방송별 task 1개 (5초 주기)
```

- 지금 `app.state.<이름>`을 직접 쓰는 코드는 `runtime(request, station_id).<이름>`으로 바꾼다.
  `StationLifecycle`은 `app` 대신 `StationRuntime`을 받는다.
- 공용으로 남는 것: Redis 클라이언트, 엔진 HTTP 클라이언트, 사용자·게시판·알림 저장소, Lavalink/YouTube 작업자.
- 엔진 상태 조회(`/v1/engine/status`)는 지금 lifecycle마다 5초에 한 번이다. 채널 3개면 15초에 3번이라
  그대로 둔다. 채널을 늘릴 때 공용 캐시로 바꾼다.
- 종료 정리 후 `HlsPublisher`를 새로 만드는 부분(`cleanup`)은 런타임의 `hls`를 바꾼다.

## 5. 방송 생성과 동시 방송 제한

`POST /api/stations` (본문·`Idempotency-Key`는 지금과 같음)

하나의 Lua 스크립트가 모든 채널의 `backend:{sid}:station` 키를 받아 원자적으로 처리한다.

1. 어떤 채널에 같은 소유자·같은 `retry_key`·같은 fingerprint 레코드가 있으면 그 레코드를 돌려준다 (재시도)
2. 이 사용자가 소유한 방송 중 `ended`가 아닌 것이 있으면 **409** `이미 진행 중인 방송이 있습니다` (응답에 그 `station_id`)
3. 레코드가 없거나 `ended`인 첫 채널에 생성 (`engine:control`에 `station.created`, 지금 CREATE와 같음)
4. 빈 채널이 없으면 **503** `모든 채널이 사용 중입니다` + `Retry-After`

- 생성 직전 지금처럼 lifecycle 잠금을 잡는데, 채널을 고르기 전이라 어느 채널 잠금인지 모른다.
  생성 전용 전역 `asyncio.Lock` 하나와 Lua의 원자성으로 충분하다(프로세스 하나 전제).
- `rejected`(엔진 거부) 방송도 종료 정리를 거쳐 `ended`가 되어야 채널이 빈다. 지금 동작과 같다.
- 응답의 `station.station_id`로 프론트가 청취 화면에 들어간다.

## 6. API

### 6.1 새로 추가

| API | 인증 | 내용 |
| --- | --- | --- |
| `GET /api/stations` | 없음 | 진행 중 방송 목록. 채널별 `station_id`, `status`, `dj_name`, `topic`, `format`, `hls_url`, `hls_ready`, `listeners`, `created_at`. `ended`·빈 채널은 빼고, 소유자 ID 같은 내부 값은 넣지 않는다 |
| `GET /api/stations/mine` | 필요 | 내가 진행 중인 방송 (없으면 `{station: null}`). 호스트 화면 복귀용 |
| `GET /api/stations/{sid}/stream/state` | 없음 | 지금의 `/api/stream/state`와 같은 응답 |
| `GET·POST /api/stations/{sid}/requests` | 지금과 같음 | 채팅·사연 목록과 등록 |
| `GET /api/stations/{sid}/requests/{id}/state`, `.../history` | 지금과 같음 | |
| `GET /api/stations/{sid}/broadcast/queue`, `.../broadcast/segments/{id}/state` | 지금과 같음 | |

### 6.2 동작이 바뀌는 기존 API

| API | 변경 |
| --- | --- |
| `/api/stream/state`, `/api/requests` 등 방송 ID 없는 경로 | 기본 채널로 동작 (응답 형식 그대로). 프론트 전환이 끝나면 지운다 `[결정 필요: 언제]` |
| `/api/stations/{sid}`, `.../stop`, `.../presence`, `.../tracks*`, `.../replays*`, `/hls/{sid}/...` | 설정한 채널이면 받는다 (지금은 하나만) |
| `/api/replays/bookmarked`, 북마크 추가·삭제, `DELETE /api/replays/{session_id}` | 세션 ID로 채널을 찾는다. 세션 ID→채널 색인 해시(`backend:replay-stations`)를 새로 둔다 |
| `/api/stations/{sid}/replays/mine` | 그대로 두고, 채널을 모은 `GET /api/replays/mine`을 추가 |
| `/api/scheduler/tick` | 디버그용. 기본 채널만 |

### 6.3 프론트엔드에 필요한 작업 (프론트 담당)

- 방송 목록 화면 (`GET /api/stations`)
- 청취 경로에 방송 ID 추가 (`/listen/:stationId`) 후 위 방송별 API 사용
- 방송 생성 후 응답의 `station_id`로 이동, 409(이미 진행 중)·503(채널 가득) 안내
- 마이페이지 "내 다시듣기"를 `GET /api/replays/mine`으로

## 7. 설정

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `ONAIR_STATION_IDS` | `ONAIR_STATION_ID` 값 하나 | 쉼표로 구분한 채널 ID. 첫 번째가 기본 채널. 각 ID는 지금 규칙(`[A-Za-z0-9_-]{1,64}`)을 따르고 중복 불가 |
| `ONAIR_STATION_ID` | `st_local_dev` | 하위 호환. `ONAIR_STATION_IDS`가 없을 때만 쓴다 |

채널 수(= 동시 방송 상한)는 EC2에서 실측해 정한다 `[결정 필요]`. 방송 하나가 쓰는 것:
FFmpeg 인코딩(세그먼트마다), HLS 디스크, LLM·TTS 호출(비용). 측정 전 기본 권장값은 3.

## 8. 하위 호환과 배포

- `ONAIR_STATION_IDS`를 설정하지 않으면 지금과 똑같이 방송 하나로 동작한다. 백엔드를 먼저 배포하고
  프론트가 바뀐 뒤 채널을 늘리면 된다.
- 기존 `st_local_dev`의 기록·다시듣기는 첫 채널로 그대로 남는다. 데이터 이전은 없다.
- 계약 버전 변경 없음.

## 9. 구현 순서 (PR 단위 제안)

1. `StationRuntime` 도입 — 동작 변화 없이 `app.state`의 방송별 객체를 런타임 하나로 묶는다 (채널 1개)
2. 채널 목록 설정과 채널별 런타임·task 시작·종료, 방송별 경로 검증을 채널 목록 기준으로
3. 생성 Lua(사용자당 1개·빈 채널 선택)와 `GET /api/stations`, `/api/stations/mine`
4. 방송별 경로(6.1) 추가, 기존 경로는 기본 채널로
5. 다시듣기 색인과 `GET /api/replays/mine`
6. 문서([BACKEND_STATION_CREATION.md](BACKEND_STATION_CREATION.md) 등) 갱신

## 10. 검증 계획

- 단위: 채널 선택 Lua — 재시도 멱등, 사용자당 1개, 채널 가득, `ended` 채널 재사용, 동시 생성 경합
- 통합(실제 Redis): 채널 2개에서 방송 2개를 동시에 열고 요청·송출·종료 정리가 서로 섞이지 않는지,
  한 방송 종료가 다른 방송 발행기를 멈추지 않는지
- 엔진 포함 통합(더미 LLM·TTS): 방송 2개 동시 송출, 각 `engine:{sid}:in`에 자기 방송 이벤트만
- 기존 테스트는 채널 1개 설정으로 그대로 통과해야 한다

## 11. 확인이 필요한 질문

1. 방송 ID 방식 A(고정 채널) / B(방송마다 새 ID) — 3절
2. 채널 수(동시 방송 상한) 초기값 — 7절
3. 진행 중 방송 목록을 로그인 없이 보여 줄지 — 6.1절
4. 기존 방송 ID 없는 경로를 언제 지울지 — 6.2절
5. 한 사용자가 방송 중 다른 방송을 청취·채팅하는 것은 허용 (제한 없음으로 가정)
