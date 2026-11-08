# 다중 방송 (백엔드 설계)

> 상태: **11월 7일 회의 결정 반영**. 남은 `[확인 필요]` 항목은 11절에 모았다.
> 구현은 #157 3단계(계약 v2 전환) 이후에 시작한다. 같은 파일(`requests.py`, `consumer.py`,
> `journal.py`, `stations.py`)을 크게 바꾸므로 동시에 진행하면 충돌이 많다.

## 1. 목표와 범위

여러 호스트가 동시에 각자 방송을 열고, 청취자는 진행 중인 방송 중 하나를 골라 듣는다.

| 정한 것 | 내용 |
| --- | --- |
| 방송 ID | **고정 채널** (3절 A안). 채널 목록을 설정으로 정하고 빈 채널에 새 방송을 연다 |
| 동시 방송 상한 | 채널 **10개 이상**. 첫 설정은 10개. 채널 수가 곧 상한이다 |
| 사용자당 제한 | 진행 중 방송 1개 |
| 방송을 만들 수 있는 사람 | 일반 회원과 어드민. 게스트는 403 (#214에서 구현) |
| 첫 화면 | 로그인. 닉네임으로 참가(게스트)와 어드민 핀 참가를 둔다 (#198, #214) |
| 기존 API | 방송 ID가 없는 경로는 **기본 채널**로 동작하게 남겨 두고, 프론트가 방송별 경로로 옮기면 바로 지운다 |
| 실행 구조 | 백엔드 프로세스 하나 안에서 방송별 런타임을 띄운다. 배포·systemd 구성은 그대로 |

포함하지 않는 것: 방송별 프로세스 분리, 여러 서버로의 수평 확장, 방송 중 상한 변경,
프론트엔드 화면(목록·선택) 구현 — 프론트 작업은 6.3절에 필요한 API만 정리한다.

엔진은 이미 `engine:control`의 `station.created`마다 방을 하나씩 띄운다
([ENGINE_ARCHITECTURE.md](ENGINE_ARCHITECTURE.md) M3). 계약(`engine:{sid}:in`/`out`)이 방송별
스트림이므로 **엔진 변경은 없다**. 다만 TTS 한도는 모든 채널이 함께 쓴다 (7.2절).

## 2. 현재 구조에서 단일 방송에 묶인 곳

| 위치 | 단일 방송 전제 |
| --- | --- |
| `config.py` | `ONAIR_STATION_ID` 하나를 시작할 때 정한다 |
| `redis_connection.py` | `HlsPublisher`, `EngineConsumer`, `StationLifecycle`, `RequestStore`, `StationStore`, `ReplaySessionStore`, `PresenceStore`, `MusicService`, 다시듣기 북마크, `StoryFeed`가 `app.state`에 하나씩 |
| `stations.py` | 생성 Lua가 키 하나(`backend:{sid}:station`)만 본다. 조회·정지는 다른 ID를 404 |
| `api.py` | `/api/requests`, `/api/stream/state`, `/api/broadcast/queue`, `/api/requests/{id}/state·history`, `/api/broadcast/segments/{id}/state`, `/api/scheduler/tick`에 방송 ID가 없다 |
| `music.py`, `replays.py`, presence, `test_controls.py`, `/hls/...` | 경로에 `{station_id}`가 있지만 설정값과 다르면 404 |
| `replays.py` `/replays/mine` | 소유자 판단을 현재 방송 레코드 하나로 한다 |
| `board.py`, `board_stories.py` | 새 글을 설정한 방송 하나에만 사연 후보로 보낸다 |
| 디스크 | HLS `hls_dir/{sid}`, 음원 `audio_dir/{sid}` — 이미 방송별 디렉터리 |
| 프론트 | `/listen`에 방송 ID가 없고 `/api/stream/state`의 `stationId`를 받아 쓴다 |

방송별 Redis 키(`backend:{sid}:*`)와 디스크 경로는 이미 방송 ID로 나뉘어 있다.
바꿀 것은 주로 **"하나만 만든다"는 연결부와 라우팅**이다.

## 3. 방송 ID 방식 (결정: A)

| | A. 고정 채널 (채택) | B. 방송마다 새 ID |
| --- | --- | --- |
| ID | 설정한 채널 목록 `ONAIR_STATION_IDS=st_ch01,...,st_ch10`. 빈 채널에 새 방송을 연다 | 생성할 때마다 `st_{랜덤}` 발급 |
| 상한 | 채널 수 = 상한 | 별도 설정값 |
| 재사용 | 지금처럼 같은 ID에 `generation`으로 새 방송 — 이미 구현·검증된 종료 정리, 엔진 스트림 보관, 이전 방송 이벤트 무시 로직을 그대로 쓴다 | 재사용이 없어 세대 구분이 필요 없지만, 런타임을 방송마다 만들고 지워야 하고 재시작 때 진행 중 방송을 찾아 복구해야 한다 |
| 런타임 | 시작할 때 채널마다 만든다. 발행기는 방송이 있을 때만 돈다(지금도 상태에 따라 start/stop) | 생성·종료 때 동적으로 만들고 지움 |
| 다시듣기 | 채널별 목록. "내 다시듣기"·북마크는 채널을 모아 응답 | 방송별로 흩어져 전역 색인이 필요 |
| 누적 | 채널 수만큼만 키·디렉터리 | 방송마다 키·디렉터리가 남아 보존 정책 필요 |
| 기존 데이터 | 기존 `st_local_dev`를 첫 채널로 두면 이전 없음 | 기존 방송을 새 구조로 옮길지 정해야 함 |

백엔드 변경을 연결부와 라우팅에 묶어 두고, 가장 복잡한 종료 정리·세대 구분 코드를 건드리지 않는다.
청취자 화면에는 채널 번호가 아니라 방송 정보(DJ 이름, 주제)를 보여 준다.

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
  story_feed    StoryFeed
  consumer      EngineConsumer        — 방송별 task 1개 (engine:{sid}:out)
  hls           HlsPublisher          — 방송이 started일 때만 실행
  lifecycle     StationLifecycle      — 방송별 task 1개 (5초 주기)
```

- 지금 `app.state.<이름>`을 직접 쓰는 코드는 `runtime(request, station_id).<이름>`으로 바꾼다.
  `StationLifecycle`은 `app` 대신 `StationRuntime`을 받는다.
- 공용으로 남는 것: Redis 클라이언트, 엔진 HTTP 클라이언트, 사용자·게시판·알림 저장소, Lavalink/YouTube 작업자.
- 엔진 상태 조회(`/v1/engine/status`)는 지금 lifecycle마다 5초에 한 번이다. 채널 10개면 5초에 10번이라
  **공용 캐시 하나로 바꾼다** (한 번 조회해 모든 채널이 나눠 쓴다).
- 종료 정리 후 `HlsPublisher`를 새로 만드는 부분(`cleanup`)은 런타임의 `hls`를 바꾼다.

### 4.1 게시판 사연

- 새 글은 진행 중인 talk 방송 **모두**에 후보로 보낸다 (`StoryFeed`를 채널마다).
- 사연을 보낸 채널은 `backend:board:story-stations`에 남긴다(보내는 Lua 안에서 함께 기록).
- 읽힘 표시(`backend:board:read-stories`)는 지금처럼 전역 하나다. 한 채널에서 읽히면
  **다른 채널에는 `board.story.withdrawn`을 보내** 같은 사연이 두 번 읽히지 않게 한다.
  엔진이 이미 제출한 조각은 그대로 나가므로, 거의 동시에 두 채널에서 읽히는 경우는 남는다.

## 5. 방송 생성과 동시 방송 제한

`POST /api/stations` (본문·`Idempotency-Key`는 지금과 같음, 게스트는 403)

생성 전용 전역 잠금 안에서 모든 채널의 `backend:{sid}:station`을 한 번에 읽고 아래 순서로 채널을 고른 뒤,
고른 채널에 지금의 단일 채널 생성 Lua를 그대로 쓴다 (3단계 구현).

1. 어떤 채널에 같은 소유자·같은 `retry_key`·같은 fingerprint 레코드가 있으면 그 레코드를 돌려준다 (재시도)
2. 이 사용자가 소유한 방송 중 `ended`가 아닌 것이 있으면 **409** `이미 진행 중인 방송이 있습니다` (응답에 그 `station_id`)
3. 레코드가 없거나 `ended`인 첫 채널에 생성 (`engine:control`에 `station.created`, 지금 CREATE와 같음)
4. 빈 채널이 없으면 **503** `모든 채널이 사용 중입니다` + `Retry-After`

- 채널을 고르기 전이라 채널별 lifecycle 잠금으로는 막을 수 없어, 생성 전용 전역 `asyncio.Lock`을 둔다.
  고른 뒤에는 그 채널의 lifecycle 잠금도 잡는다. 백엔드 프로세스가 하나라는 전제다.
- 고르는 동안 다른 채널 기록은 `ended`로만 바뀔 수 있어(채널이 비는 방향) 결과가 틀어지지 않는다.
- `rejected`(엔진 거부) 방송도 종료 정리를 거쳐 `ended`가 되어야 채널이 빈다. 지금 동작과 같다.
- 응답의 `station.station_id`로 프론트가 청취 화면에 들어간다.

## 6. API

첫 화면이 로그인이므로 새 목록·요청 경로는 로그인을 요구한다. 게스트도 청취·입력·게시판은 쓴다.
방송 상태(`stream/state`), presence, `/hls/...`는 지금처럼 토큰 없이 받는다 (오디오 요청에 토큰을 붙이기 어렵다).

### 6.1 새로 추가

| API | 내용 |
| --- | --- |
| `GET /api/stations` | 진행 중 방송 목록. 채널별 `station_id`, `status`, `dj_name`, `topic`, `format`, `hls_url`, `hls_ready`, `listeners`, `created_at`, 내 방송 여부 `mine`. `ended`·빈 채널은 빼고, 소유자 ID 같은 내부 값은 넣지 않는다 |
| `GET /api/stations/mine` | 내가 진행 중인 방송 (없으면 `{station: null}`). 호스트 화면 복귀용 |
| `GET /api/stations/{sid}/stream/state` | 지금의 `/api/stream/state`와 같은 응답 |
| `GET·POST /api/stations/{sid}/requests` | 채팅·사연 목록과 등록. 응답은 #214와 같이 `listenerId` 대신 `mine` |
| `GET /api/stations/{sid}/requests/{id}/state`, `.../history` | 지금과 같음 |
| `GET /api/stations/{sid}/broadcast/queue`, `.../broadcast/segments/{id}/state` | 지금과 같음 |

### 6.2 동작이 바뀌는 기존 API

| API | 변경 |
| --- | --- |
| `/api/stream/state`, `/api/requests` 등 방송 ID 없는 경로 | 기본 채널로 동작 (응답 형식 그대로). **프론트가 6.1 경로로 옮긴 다음 배포에서 지운다** |
| `/api/stations/{sid}`, `.../stop`, `.../presence`, `.../tracks*`, `.../replays*`, `.../corner-skip`, `.../decisions`, `/hls/{sid}/...` | 설정한 채널이면 받는다 (지금은 하나만) |
| `/api/replays/bookmarked`, 북마크 추가·삭제, `DELETE /api/replays/{session_id}` | 세션 ID로 채널을 찾는다. 세션 ID→채널 색인 해시(`backend:replay-stations`)를 새로 둔다. 색인 전에 만든 세션은 채널마다 찾아보고, 어디에도 없으면 기본 채널을 쓴다. 북마크 목록은 채널별 북마크를 북마크 시각순으로 합친다 |
| `/api/stations/{sid}/replays/mine` | 그대로 두고, 채널을 모은 `GET /api/replays/mine`을 추가 |
| `/api/scheduler/tick` | 디버그용. 기본 채널만 |

### 6.3 프론트엔드에 필요한 작업 (프론트 담당)

- 방송 목록 화면 (`GET /api/stations`). 지금 메인 화면은 목업 데이터다
- 청취 경로에 방송 ID 추가 (`/listen/:stationId`) 후 위 방송별 API 사용
- 방송 생성 후 응답의 `station_id`로 이동, 409(이미 진행 중)·503(채널 가득) 안내
- 마이페이지 "내 다시듣기"를 `GET /api/replays/mine`으로
- 테스트 화면(`/test`)이 쓰는 방송 ID도 목록에서 고른다 (테스트 화면 담당)

## 7. 설정과 용량

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `ONAIR_STATION_IDS` | `ONAIR_STATION_ID` 값 하나 | 쉼표로 구분한 채널 ID. 첫 번째가 기본 채널. 각 ID는 지금 규칙(`[A-Za-z0-9_-]{1,64}`)을 따르고 중복 불가. 운영 첫 설정은 10개 |
| `ONAIR_STATION_ID` | `st_local_dev` | 하위 호환. `ONAIR_STATION_IDS`가 없을 때만 쓴다 |

### 7.1 서버 자원 `[확인 필요]`

방송 하나가 쓰는 것: FFmpeg 인코딩(세그먼트마다), HLS 디스크, 엔진의 생성 작업(방송마다 동시 2개).
10개를 EC2 한 대에서 버티는지는 측정하지 않았다. 구현 2단계가 끝나면 더미 LLM·TTS로 10채널을 동시에
열어 CPU·메모리·세그먼트 지연을 잰다.

### 7.2 Gemini TTS 한도

엔진은 `gemini-3.8-flash-tts`를 쓴다. 프로젝트 한도(Tier 2, 2026-11-08 확인):

| RPM | TPM | RPD |
| --- | --- | --- |
| 1,000 | 100,000 | 10,000 |

- 공식 문서 기준으로 한도는 **프로젝트 단위**이고, 하루 한도는 **태평양 시간 자정**(한국 시간 오후 4~5시)에
  초기화된다. 채널 10개가 **이 한도를 나눠 쓴다**.
- 개발자 포럼에는 Tier 2에서 화면에는 RPD 10,000이 보이는데 API는 RPD 100에서 429를 냈다는 보고가 있다
  (공식 답변 없음). 시연 전에 하루 100번을 넘겨 호출해 실제로 막히지 않는지 한 번 확인한다.

코드로 추정한 사용량 (실측 아님):

- TTS 호출은 멘트 조각 하나에 1번, 방송 시작 때 고정 문구마다 1번(동시 3개)이다. 실패하면 1번 재시도한다.
- 코너 시간 설정과 곡 길이로 보면 방송 1시간에 **대략 50~100번**이다. 채널마다 평균 분당 1~2번이고,
  여러 채널이 함께 시작하면 고정 문구 때문에 잠깐 몰린다.
- 채널 10개가 1시간씩 방송하면 **500~1,000번**이다.
- 한 번에 보내는 대본은 수백 토큰이다. 10채널이 분당 20번 불러도 1만 토큰 안팎이다.

판단:

- **RPM, TPM:** 10채널이 동시에 방송해도 한도의 몇 % 수준이라 충분하다.
- **RPD:** 하루에 10채널 1시간 방송을 10번 정도 할 수 있다. 시연과 테스트에는 충분하지만, 채널을 상시
  돌리면(10채널 × 하루 종일) 모자란다.

엔진은 429를 받으면 안내된 시간까지 TTS와 LLM을 부르지 않는다. 하루 한도에 걸리면 초기화 시각까지
**모든 채널이 대체 멘트로만 이어진다**. 한도에 가까워지면 채널 수나 하루 방송 횟수를 줄인다.

## 8. 하위 호환과 배포

- `ONAIR_STATION_IDS`를 설정하지 않으면 지금과 똑같이 방송 하나로 동작한다. 백엔드를 먼저 배포하고
  프론트가 바뀐 뒤 채널을 늘리면 된다.
- 기존 `st_local_dev`의 기록·다시듣기는 첫 채널로 그대로 남는다. 데이터 이전은 없다.
- 계약 버전 변경 없음.

## 9. 구현 순서 (PR 단위 제안)

1. `StationRuntime` 도입 — 동작 변화 없이 `app.state`의 방송별 객체를 런타임 하나로 묶는다 (채널 1개) — 완료
2. 채널 목록 설정과 채널별 런타임·task 시작·종료, 방송별 경로 검증을 채널 목록 기준으로, 엔진 상태 조회 공용 캐시 — 완료
3. 생성 Lua(사용자당 1개·빈 채널 선택)와 `GET /api/stations`, `/api/stations/mine` — 완료
4. 방송별 경로(6.1) 추가, 기존 경로는 기본 채널로 — 완료
5. 게시판 사연을 모든 talk 채널로 보내고 읽히면 다른 채널에서 회수 (4.1절) — 완료
6. 다시듣기 색인과 `GET /api/replays/mine` — 완료
7. 프론트 전환 뒤 방송 ID 없는 경로 삭제, 문서([BACKEND_STATION_CREATION.md](BACKEND_STATION_CREATION.md) 등) 갱신

## 10. 검증 계획

- 단위: 채널 선택 Lua — 재시도 멱등, 사용자당 1개, 게스트 거부, 채널 가득, `ended` 채널 재사용, 동시 생성 경합
- 통합(실제 Redis): 채널 2개에서 방송 2개를 동시에 열고 요청·송출·종료 정리가 서로 섞이지 않는지,
  한 방송 종료가 다른 방송 발행기를 멈추지 않는지, 한 채널에서 읽힌 사연이 다른 채널에서 회수되는지
- 엔진 포함 통합(더미 LLM·TTS): 방송 2개 동시 송출, 각 `engine:{sid}:in`에 자기 방송 이벤트만
- 용량: 더미 LLM·TTS로 10채널 동시 송출 (7.1절)
- 기존 테스트는 채널 1개 설정으로 그대로 통과해야 한다

## 11. 남은 확인 사항

1. Gemini TTS가 실제로 하루 100번에서 막히지 않는지 — 시연 전 한 번 확인 (7.2절)
2. EC2 한 대가 10채널을 버티는지 — 2단계 뒤 측정 (7.1절)
3. 한 사용자가 방송 중 다른 방송을 청취·채팅하는 것은 허용 (제한 없음으로 가정)
