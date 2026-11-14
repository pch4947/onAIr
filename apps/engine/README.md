# onAIr 대본 엔진 (`apps/engine`)

편성 관리자가 방송 형식(음악·사연토크·뉴스)의 클락과 코너 정의에 따라 LLM 대본을 생성하고 TTS 오디오를 백엔드에 제출하는 파트입니다.

- 설계 문서: [docs/ENGINE_ARCHITECTURE.md](../../docs/ENGINE_ARCHITECTURE.md), [docs/CORNER_DESIGN.md](../../docs/CORNER_DESIGN.md)
- 현재 상태: **M3 — 형식·클락·코너 구조** (#105). 청취자 입력 라우팅·선별기는 M4 — 그 전까지 요청은 채팅 인용을 허용하는 코너의 다음 멘트에 인용됩니다(과도기). 요청별 접수 확인(ack) 멘트는 없습니다. 더미 어댑터는 키 없는 테스트·폴백용으로 유지.

## 시작하기

```bash
cd apps/engine
python -m venv .venv
.venv\Scripts\activate        # Windows (bash: source .venv/Scripts/activate)
pip install -e ../../packages/onair_schema   # 백엔드와 같이 쓰는 persona·station.created 스키마
pip install -e ".[dev,redis,api]"   # redis: Redis 전송, api: 엔진 REST
Copy-Item .env.example .env   # bash: cp .env.example .env
```

### API 키 (`.env`)

LLM·TTS 키는 `apps/engine/.env`에 넣습니다. 엔진이 시작할 때 읽고, 이미 설정된 OS 환경변수가 우선합니다(백엔드 `apps/backend/.env`와 같은 방식). `.env`는 커밋되지 않습니다. 키 목록은 [.env.example](.env.example)에 있습니다.

```ini
OPENAI_API_KEY=sk-...
GOOGLE_TTS_API_KEY=...
```

더미 LLM·TTS만 쓸 때는 키가 필요 없습니다.

## 실행

```bash
onair-engine --config config/station.example.yaml --max-segments 6 --demo-request "요즘 잠이 안 와요"
```

- 세그먼트 제출이 `SUBMIT {...}` JSON 라인으로 출력됩니다 (백엔드 계약 확정 전까지의 stdout 더미).
- 생성된 오디오는 `var/audio/`, 계측 로그는 `var/engine_metrics.sqlite`에 쌓입니다.
- `--max-segments` 없이 실행하면 엔딩까지 진행한 뒤 끝납니다 (Ctrl+C로 중단).
- 곡은 백엔드가 `track.queued`로 알려 준 곡을 소개합니다(`music_ref`). 곡 길이만큼 버퍼가 차므로 다음 멘트까지 기다립니다.
- 로컬 실행은 백엔드 곡이 없으면 더미 곡 이름으로 소개 멘트만 만듭니다(`music_ref`로 보내지 않고 방송 시간에도 넣지 않음). 운영(`--serve`)은 더미 곡을 쓰지 않습니다 — 등록된 곡이 없으면 곡 소개 코너를 건너뛰고, 음악 형식은 곡이 들어올 때까지 기다립니다. 설정 파일 `pipeline.dummy_tracks`로도 끌 수 있습니다.
- 생성된 대본은 `SCRIPT seg_xxx [코너] 대본...` 라인으로 함께 출력됩니다.
- 방송 형식은 설정 파일 `station.format` 또는 `--format music|talk|news`로 고릅니다. 코너와 순환 틀은 [config/formats/](config/formats/)에 있습니다 — 코너를 고치거나 더할 때 코드는 건드리지 않습니다.
- 데모 요청은 채팅 인용을 허용하는 첫 단계(사연토크면 오프닝의 코너 예고)에서 인용됩니다. 뉴스 형식은 청취자 입력을 방송에 반영하지 않습니다.
- 사연 코너(게시판 사연)·문자 소개(사연 보내기)는 M4, DJ 대 청취자 게임(투표)은 M5부터 진행됩니다. 그 전에는 사연 코너가 주제 토크로 대체되고 나머지는 건너뜁니다. 뉴스 형식은 [config/news_feeds.yaml](config/news_feeds.yaml)의 RSS 피드에서 호스트가 고른 분야만 수집합니다. 수집에 실패하면 기사 코너를 건너뜁니다. 피드를 넣기 전에 매체의 이용 조건을 확인하세요.

### 더미 대본을 실제 음성으로 듣기

더미 LLM은 단계별로 방송처럼 들리는 고정 대본을 반환합니다. `--tts edge`를 주면 Microsoft Edge 온라인 TTS(API 키 불필요, 인터넷 필요)가 대본을 한국어로 읽어 mp3로 저장합니다.

```bash
pip install -e ".[tts]"
onair-engine --tts edge --max-segments 15 --demo-request "요즘 잠이 안 와요"
```

- 오디오: `var/audio/st_local_dev/seg_xxx.mp3` (코너 콜 같은 접착 문구는 `glue/`)
- 보이스 변경: 설정 파일 `pipeline.tts_voice` (기본 `ko-KR-SunHiNeural`, 남성 `ko-KR-InJoonNeural`)
- API 키 없이 듣는 청취 테스트용 어댑터입니다. 비공식 엔드포인트이므로 운영에는 쓰지 않습니다 (운영은 Gemini).

### Gemini TTS (운영 — 2026-10-30 확정)

1. [Google AI Studio](https://aistudio.google.com/apikey)에서 API 키를 만들어 `.env`의 `GEMINI_API_KEY`에 넣습니다. Gemini LLM 어댑터와 같은 키입니다.
2. 보이스 목록을 보고 쓸 보이스 ID를 고릅니다.
3. 설정 파일 `pipeline.tts_voice`에 그 ID를 넣고 실행합니다. 비우면 `kore`로 합성합니다 (운영에서는 방마다 `persona.voice`를 씁니다).

```powershell
onair-engine --list-voices          # ID<탭>이름<탭>성별<탭>한국어 여부
onair-engine --tts gemini --max-segments 15 --demo-request "요즘 잠이 안 와요"
```

- 모델은 `gemini-3.8-flash-tts`, 출력은 WAV(24kHz, mono, 16-bit)입니다. 언어는 입력에서 자동 감지합니다.
- 추가 의존성은 없습니다 (표준 라이브러리 REST 호출).
- 스타일 지시(`voice_style`)·voice design은 아직 없습니다. 채팅 표기(`ㅋㅋㅋ` 등)는 합성 직전에 엔진이 바꿉니다(`pipeline/normalize.py`). 생성 지연도 아직 실측하지 않았습니다.

### Cartesia TTS (비교 측정용)

1. [play.cartesia.ai/keys](https://play.cartesia.ai/keys)에서 API 키를 만들어 `.env`의 `CARTESIA_API_KEY`에 넣습니다.
2. 설정 파일 `pipeline.tts_voice`에 Cartesia 보이스 ID를 넣고 실행합니다. 기본값은 없습니다.

```powershell
onair-engine --tts cartesia --max-segments 15 --demo-request "요즘 잠이 안 와요"
```

- 모델은 날짜 고정 스냅샷 `sonic-3.6-2026-08-27`, 언어 `ko`, 출력 mp3(44.1kHz 128kbps)입니다. 실험 중 모델이 바뀌지 않도록 별칭(`sonic-3.6`)을 쓰지 않습니다.
- 추가 의존성은 없습니다 (표준 라이브러리 REST 호출).
- 채팅의 `ㅋㅋㅋ`는 "엑엑엑"으로, `ㅠㅠ`·이모지는 읽지 않습니다. TTS 전 텍스트 정규화는 별도 과제입니다.

### 채팅 반영 정책

채팅은 도착할 때 규칙으로 반응형·독립형을 나누고 그 순간 방송에 나가던 것(앵커)을 기록한 뒤 풀에 쌓습니다. 인용할 수 있는 단계를 만들 때 선별기가 고릅니다 ([CORNER_DESIGN.md](../../docs/CORNER_DESIGN.md) 5.1.3, 5.7, 5.8절).

- 수치와 표현 목록: `config/chat_rules.yaml` (설정 파일 `pipeline.chat_rules`로 경로 변경). 대부분 검증 전 임시값입니다.
- 선별 기준: 일반 방은 화제 관련성 우선, 실험용 방은 `experiment.selector`·`window_sec`.
- 지표: 계측 SQLite의 `input_log`·`selection_log`·`reflection_log`와 `telemetry.reflection_summary()`.
- 만료(`expired`)는 백엔드에 통보합니다. 반영 확정(`reflected`)은 계약 v2 전까지 엔진 계측에만 남습니다.

### 입력 라우팅

입력은 종류별 저장소로 갑니다 (`inputs/`). `story`·`mood`·`chat`은 채팅, `message`는 문자 inbox, `board.story`는 사연 풀, `song_request`는 곡을 기다리는 목록입니다. 형식이 받지 않는 입력은 바로 종결합니다 (뉴스의 채팅, 음악의 문자).

- **문자**: `text_corner`가 도착 순으로 낭독합니다. 대본에 글이 빠지면 한 번 되돌리고, 또 빠지면 `rejected`입니다.
- **게시판 사연**: 받은 사연을 모아 LLM으로 주제 관련도를 판정하고, 관련 있는 사연만 `story_corner` 후보로 남깁니다. LLM이 실패하면 주제와 단어가 겹치는 사연만 남기고, 더미 LLM은 모두 남깁니다. 결과는 `story_judge_log`에 기록합니다. 후보 중에서는 주제 일치, 최근 순으로 고릅니다. 제출의 `request_ref`는 `story_id`입니다.
- **신청곡 한마디**: 같은 `request_id`의 `track.queued`가 오면 곡 소개에 넣습니다.
- **계약 버전**: 엔진이 백엔드로 보내는 이벤트의 버전은 `transport.contract_version`(환경변수 `ONAIR_CONTRACT_VERSION`이 먼저)으로 정합니다. 기본값 1은 지금까지와 같은 형식입니다. 2로 올리면 `segment.submitted`가 `request_ref` 대신 `input_refs`·`reflection_levels`를 싣고, `request.state`가 `pooled`·`inboxed`·`selected`·`reflected` 같은 v2 값으로 나갑니다(계약 10.4·10.5절). 방송 동작도 하나 달라집니다 — 한 멘트에 입력을 여러 건 싣습니다(참여 강도 `high`는 채팅 2건 — 오프닝은 1건, 사연·신청곡 소개 멘트에는 채팅 1건을 함께, #267). 백엔드가 v1·v2를 함께 받게 배포된 뒤에 올리고, 문제가 생기면 1로 되돌립니다(전환 순서는 계약 10.10절). 받는 쪽은 버전을 가리지 않습니다.
- **코너 알림**: `pipeline.emit_corner_changed`로 켜고 끕니다. `config/station.example.yaml`에서 켜 두었고, VM의 엔진 서비스가 이 파일을 읽습니다. 코드 기본값은 꺼짐입니다. `corner.changed`를 모르는 백엔드(PR #189 이전)는 이벤트를 격리하므로 그런 백엔드와는 끕니다.
- 백엔드가 모르는 상태는 바꿔 보내거나 보내지 않습니다 (`domain.wire_state`).

### 코너 변경 (#259)

테스트 화면의 코너 버튼은 백엔드를 거쳐 `segment.discarded`와 `corner.skip`으로 옵니다 (계약 3.2절).

- **`corner.skip`**: 지금 코너를 끝내고 고른 코너로 들어갑니다. 그 뒤는 원래 순환으로 돌아갑니다.
  - 생성 중이거나 아직 제출하지 않은 멘트는 내보내지 않습니다. 쓰던 곡과 입력은 되돌리고, 입력의 실패 횟수로 세지 않습니다.
  - 버린 생성은 끝까지 돌지만 새 코너가 그 자리를 기다리지 않습니다. 그동안 동시 생성이 설정값의 두 배까지 늘 수 있습니다.
  - 진입 조건을 못 채우면(문자 0건, 다음 곡 없음) 대체 코너로 가거나 건너뛰고 순환을 잇습니다. 곡 연속 방지는 적용하지 않습니다.
  - 형식에 없는 코너, 오프닝·엔딩, 엔딩이 시작된 뒤의 요청은 무시합니다.
- **`segment.discarded`**: 백엔드가 재생하지 않고 버린 멘트의 입력과 곡을 되돌리고, 그 길이를 버퍼 계산에서 뺍니다.
- 엔진 패널에는 `코너 변경 요청 · 문자 소개`, `코너 변경 무시 · … (사유)`, `미송출 멘트 3개 버림 · 입력 1건 되돌림`으로 남습니다.
- 버튼을 눌러도 재생 중인 것과 백엔드가 미리 인코딩한 분량은 그대로 나갑니다. 곡이 나가는 중이면 곡이 끝나야 바뀝니다.
- 문자·사연·한마디는 백엔드가 보내기 시작해야 실제 방송에서 쓰입니다 (#168, #169, #140).

### 페르소나 전환 (#261)

테스트 화면의 페르소나 버튼은 백엔드를 거쳐 `segment.discarded`와 `station.persona_changed`로 옵니다 (계약 3.2절).

- **검사**: 방 생성 때와 같은 기준으로 봅니다 — 형식, 보이스 목록, 안전 규칙(L0), 연결 멘트 문구(DJ 이름).
  - 걸리면 바꾸지 않고 이전 페르소나로 계속 방송합니다. 백엔드에 알리는 이벤트는 없고, 엔진 패널에 `페르소나 전환 거절 · 사유`가 남습니다.
  - 같은 `persona_id`가 다시 오면 무시합니다.
- **전환**: 편성 루프의 다음 차례(0.5초 안)에 대본 프롬프트와 TTS 목소리를 바꿉니다.
  - 생성 중이거나 아직 제출하지 않은 멘트는 이전 DJ의 것이라 내보내지 않습니다. 쓰던 곡과 입력은 되돌리고, 입력의 실패 횟수로 세지 않습니다.
  - 하던 말 코너는 새 DJ가 처음부터 다시 합니다. 이전 DJ가 써 둔 이야기 덩어리를 새 목소리로 잇지 않습니다.
  - 곡 소개를 만들던 중이었으면 다시 하지 않습니다. 새 DJ가 말부터 하고, 그 곡은 다음 곡 차례에 나옵니다.
  - 직전 멘트 맥락은 비웁니다. 이전 DJ의 말투가 새 DJ의 대본으로 옮아가지 않게 합니다.
  - 새 DJ는 인용 간격과 같은 청취자 연속 금지를 새로 셉니다. 같은 채팅을 다시 보내도 바로 인용할 수 있습니다.
  - 주제, 남은 방송 시간, 순환 차례, 방송된 청취자 이야기(다시 꺼내기·엔딩 돌아보기)는 그대로입니다.
- **연결 멘트**: 새 목소리로 뒤에서 다시 합성합니다. 끝날 때까지는 연결 멘트 없이 코너에 들어갑니다.
  - 문구는 형식 파일의 고정 문장이라 DJ 말투를 따르지 않습니다(반말 DJ도 존댓말 문구를 읽습니다).
  - 합성에 실패하면 합성된 문구만 쓰고 방송은 이어집니다.
- 엔진 패널에는 `페르소나 전환 · 고요 → 번개 · 만들던 멘트 2개 버림`, `연결 멘트 9개 다시 합성 · 14.2초`로 남습니다.
- 받은 persona는 계측(`station_log`의 `station.persona_changed`)에 사본으로 남습니다.
- 버튼을 눌러도 재생 중인 것과 백엔드가 미리 인코딩한 분량은 이전 DJ의 목소리로 나갑니다. 곡이 나가는 중이면 곡이 끝나야 새 DJ가 들립니다.

### 형식 전환 (#260)

테스트 화면의 스테이션 버튼은 백엔드를 거쳐 `segment.discarded`와 `station.format_changed`로 옵니다 (계약 3.2절).

- **검사**: 방 생성 때와 같은 기준으로 봅니다 — 형식, 그 형식에서 고를 수 있는 코너, 분야.
  - 걸리면 바꾸지 않고 이전 형식으로 계속 방송합니다. 엔진 패널에 `형식 전환 거절 · 사유`가 남습니다.
  - 지금과 같은 형식·코너·분야가 다시 오면 무시합니다.
- **전환**: 편성 루프의 다음 차례(0.5초 안)에 클락과 코너 정의를 새 형식으로 바꿉니다.
  - 오프닝은 다시 하지 않고 새 형식의 순환으로 들어갑니다. 음악 형식은 곡보다 아티스트 이야기를 먼저 합니다(형식 파일의 `entry_corner`).
  - 생성 중이거나 아직 제출하지 않은 멘트는 내보내지 않고, 쓰던 곡과 입력을 되돌립니다.
  - 페르소나, 주제, 남은 방송 시간, 이야기 갈래, 방송된 청취자 이야기는 그대로입니다.
- **입력**: 새로 오는 입력은 새 형식의 계약으로 받습니다. 뉴스는 청취자 입력을 반영하지 않고, 음악은 문자를 받지 않습니다.
  - 쌓여 있던 입력은 그대로 둡니다. 문자·사연·신청곡은 그것을 읽는 형식으로 돌아오면 쓰이고, 채팅은 인용 창이 지나면 만료됩니다.
  - 게시판 사연(`board.story`)은 형식과 상관없이 받아 사연 풀에 둡니다. 백엔드는 같은 사연을 방송마다 한 번만 보냅니다.
- **뉴스**: 이벤트를 받으면 바로 기사 수집을 시작합니다. 편성은 첫 수집이 끝날 때까지 최대 8초 기다립니다. 그동안은 백엔드에 남은 분량이 나갑니다.
  - 8초 안에 끝나지 않으면 기다리지 않습니다. 기사가 없는 동안 뉴스 코너는 건너뛰고, 들어오는 대로 들어갑니다.
- **연결 멘트**: 새 형식의 문구로 뒤에서 다시 합성합니다. 끝날 때까지는 연결 멘트 없이 코너에 들어갑니다.
- **화면**: 설정 `pipeline.emit_corner_format`을 켜면 `corner.changed`에 `format`을 실어, 화면이 새 형식의 첫 멘트가 들릴 때 바뀌게 할 수 있습니다. 백엔드가 이 필드를 받게 배포된 뒤에 켭니다(기본 꺼짐 — 계약 10.7절).
- 엔진 패널에는 `형식 전환 · 사연토크 → 뉴스 · 만들던 멘트 2개 버림`, `기사 수집 · IT 4건, 경제 3건`, `코너 진입 · 헤드라인`으로 남습니다.
- 버튼을 눌러도 재생 중인 것과 백엔드가 미리 인코딩한 분량은 이전 형식으로 나갑니다. 곡이 나가는 중이면 곡이 끝나야 새 형식이 들립니다.

### 뉴스 대본의 주관 표현 규칙 (#286)

뉴스 형식은 DJ의 의견·평가·전망 없이 사실만 전합니다 (CORNER_DESIGN 6.4절). 프롬프트 지시만으로는 결정적으로 지킬 수 없어 출력에 규칙을 겁니다.

- **대상**: 뉴스 내용을 전하는 단계(`news_headline`, `news_item`, `news_recap`)의 대본입니다. 뉴스 형식의 오프닝·엔딩 인사와 곡 소개에는 걸지 않습니다.
- **규칙** (`config/safety_rules.yaml`의 `news_subjective`, `news_forecast`): 문장마다 봅니다.
  - 평가·감정("다행히", "인상적"), 지켜보기·궁금("지켜보겠습니다"), 바람·권유("~면 좋겠습니다"), 판단("중요하겠네요"), 추측("~것 같아"), 1인칭 의견, 청취자에게 묻기, 반응하는 말끝("~네요", "~구나")
  - 출처 없는 전망("~것으로 보입니다"). 같은 문장에 출처("~에 따르면", "~라고 밝혔다")가 있으면 통과합니다.
  - DJ의 입버릇. 뉴스에서는 쓰지 않습니다.
- **걸지 않는 것**: 남의 말을 옮긴 문장("유족은 안타깝다고 말했습니다")과 따옴표 안(작품·프로그램 이름, 인용문).
- **걸렸을 때**: 걸린 표현을 알려 주고 한 번 다시 씁니다. 또 걸리면 걸린 문장만 빼고 내보냅니다. 남는 문장이 없으면 그 멘트는 내보내지 않습니다(그 기사는 건너뜁니다).
- **기록**: 계측의 `subjective_log`에 걸린 문장·묶음·표현이 남고, 엔진 패널의 생성 줄에 `주관 표현 → 다시 씀`, `주관 표현 문장 뺌`, `주관 표현 차단`으로 보입니다.
- 규칙을 고칠 때는 사실만 전한 문장이 걸리지 않는지 먼저 봅니다. `tests/test_news_subjectivity.py`에 실제 피드로 뽑은 문장이 들어 있습니다.

### Google Cloud TTS

1. Google Cloud 콘솔에서 **Cloud Text-to-Speech API**를 사용 설정하고, API 키를 만들어 이 API로만 제한합니다.
2. 키를 `.env`의 `GOOGLE_TTS_API_KEY`에 넣고 실행합니다 (키는 설정 파일·커밋에 넣지 않습니다).

```powershell
onair-engine --tts google --max-segments 15 --demo-request "요즘 잠이 안 와요"
```

- 기본 보이스는 `ko-KR-Chirp3-HD-Aoede`(여성)입니다. `pipeline.tts_voice`로 바꿉니다 (남성: `ko-KR-Chirp3-HD-Charon`).
- 추가 의존성은 없습니다 (표준 라이브러리 REST 호출).
- 다른 제공자(유료 모델, 로컬 오픈소스 TTS)로 옮길 때는 `pipeline/tts.py`에 `TtsClient` 프로토콜(`file_ext`, `cache_namespace`, `synthesize`)을 지키는 어댑터를 추가하고 `make_tts`에 등록하면 됩니다.

### 실제 LLM으로 대본 생성

LLM은 OpenAI `gpt-6-luna`(`--llm openai`)로 확정됐습니다(확인 3). Claude·Gemini 어댑터는 비교용으로 남겨 두었습니다. 추가 의존성은 없습니다 (표준 라이브러리 REST 호출).

| `--llm` | 키 (`.env`) | 기본 모델 (`pipeline.llm_model` / `--llm-model`로 변경) |
|---|---|---|
| `claude` | `ANTHROPIC_API_KEY` | `claude-haiku-4-5` |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash` (thinking 끔) |
| `openai` | `OPENAI_API_KEY` (호환 서버는 선택) | 없음 — `--llm-model` 필수 |

`openai`는 Chat Completions 호환이라 `--llm-base-url`만 바꾸면 다른 서버에도 붙습니다.

```powershell
# OpenAI — 모델 ID는 OpenAI 콘솔의 값을 그대로
onair-engine --llm openai --llm-model <모델 ID> --max-segments 6
# Qwen (알리바바 DashScope) — 키는 OPENAI_API_KEY에 DashScope 키를 넣는다
onair-engine --llm openai --llm-base-url https://dashscope-intl.aliyuncs.com/compatible-mode/v1 --llm-model <qwen 모델>
# Ollama (로컬, 키 불필요)
onair-engine --llm openai --llm-base-url http://localhost:11434/v1 --llm-model qwen3:8b
```

- 추론 모델은 출력 한도를 추론 토큰에 먼저 씁니다. 지연이 길거나 빈 응답 오류가 나면 `--llm-reasoning-effort low`(설정 파일은 `pipeline.llm_reasoning_effort`)로 줄입니다.
- 쓸 수 있는 모델 ID는 제공자마다 다르고 자주 바뀝니다. 호환 서버는 `GET {base_url}/models`로 목록을 확인할 수 있습니다.
- LLM API 연동 테스트 (2026-10-05): `openai` 어댑터로 코너 4종과 데모 요청 2건을 실제 LLM으로 생성해 Google TTS까지 확인했다.

```powershell
# .env에 ANTHROPIC_API_KEY를 넣은 뒤
onair-engine --llm claude --max-segments 15 --demo-request "요즘 잠이 안 와요"
```

**LLM → TTS → 백엔드 전체 파이프라인** (Redis 서버 필요, 아래 Redis 전송 참고):

```powershell
# .env에 ANTHROPIC_API_KEY, GOOGLE_TTS_API_KEY를 넣은 뒤
onair-engine --llm claude --tts google --transport redis --demo-request "요즘 잠이 안 와요"
```

- 대본은 그대로 TTS에 들어가므로 시스템 프롬프트가 마크다운·이모지·지문·화자 표기를 금지하고, 새어 나온 것은 `clean_script`가 지웁니다. 출력 한도에서 잘리면 마지막 완결 문장까지만 씁니다.
- 부적합 판단(L1)은 `REJECT`로 통일합니다. 제공자 안전 필터 차단(Gemini `blockReason`, Claude `refusal`)도 같게 다룹니다.
- 최근 N개 대본(`context.recent_segments`)을 시스템 프롬프트에 붙여 같은 인사·표현 반복을 막습니다.
- **이야기 갈래**: 호스트가 주제를 정한 방송은 시작할 때 LLM 호출 1번으로 주제를 갈래(약 10분에 1개, 말할 거리 4~6개)로 펼칩니다. 주제 토크는 한 번 들어와서 말할 거리를 셋까지 쓰고, 남았으면 곡 뒤에 같은 갈래를 잇습니다. 다 쓰면 다음 갈래로 넘어가며 앞 갈래에서 넘어온다고 말합니다. 기록은 `topic_plan_log`와 엔진 패널에 남습니다
- **말할 거리가 있을 때만 이야기**: 주제 토크의 이야기 단계는 매번 말할 거리 하나를 씁니다. 순서는 인용할 채팅, 방송에 나간 청취자 이야기 다시 꺼내기(3분 뒤, 코너당 1번), 갈래의 남은 말할 거리입니다. 떨어지면 곡으로 갑니다 — 조용한 방송은 말이 짧아집니다. 엔딩 돌아보기도 방송에 나간 청취자 이야기와 다룬 갈래를 씁니다. 리포트 '생성' 절에 '이야기 단계 소재' 분포가 나옵니다. 설계: CORNER_DESIGN 7.7절.
- **청취자가 적은 방송**: 채팅 후보가 없고 마지막 채팅 뒤로 3분(`quiet_after_sec`)이 지나면 DJ가 끌고 갑니다 (docs/CORNER_DESIGN.md 7.7.1절)
  - 곡만 사이에 두고 주제 토크로 돌아오면 "다시 …로 돌아왔어요"를 틀지 않고, 첫 멘트가 방금 나간 곡 뒤를 잇습니다
  - 곡 사이 말 전체를 LLM 한 번으로 씁니다(문단 `block_parts`개). 문단 하나가 세그먼트 하나로 합성됩니다. 쓰는 동안 온 채팅은 남은 문단 바로 뒤에 인용합니다
  - 청취자에게 묻는 것은 10분(`question_gap_sec`)에 한 번이고, 세 번에 한 번(`song_story_every`)은 주제 대신 방금 나간 곡 이야기를 합니다
  - 채팅이 오면 멘트를 하나씩 만드는 평소 흐름으로 돌아갑니다. `block_parts: 1`이면 조용해도 한 번에 쓰지 않습니다
- **앞 멘트를 보고 씁니다**: 대본은 한 번에 하나씩 쓰고 나오는 대로 맥락에 넣습니다(합성은 동시에). 예전에는 합성이 끝나야 들어가서 다음 멘트의 절반이 앞 멘트를 모르고 썼습니다
- **곡 연속 방지** (사연토크): 곡 사이 코너가 다 빠지면(문자가 없어 문자 소개가 빠지는 등) 다음 곡도 건너뜁니다. 연달아서는 두 번까지만 건너뛰고, 신청곡은 건너뛰지 않습니다. 형식 설정 `songs_back_to_back`, 계측 `corner_log`의 `hold`. 설계: CORNER_DESIGN 6.2절.
- **실패 처리**: LLM/TTS 호출은 1회 재시도하고, 재실패하면 더미의 고정 filler 대본으로 대체해 방송을 잇습니다(곡 소개였다면 곡 `music_ref`는 그대로 싣습니다). 제공자 장애로 인용이 대체된 요청은 `queued`로 되돌리고 창이 지나면 `expired`로 끝납니다. 대본에 인용이 빠지면 한 번 되돌리고, 또 빠지면 `rejected`로 통보합니다. Gemini TTS가 429를 주면 재시도하지 않고, 안내된 시간까지 TTS와 LLM을 호출하지 않습니다(`decision_log`의 `tts_blocked`). 계측 `generation_log`에 `error`/`fallback`/`quote` 단계가 남습니다.
- **제출 순서**: 생성은 병렬(`max_concurrent_generations`)이지만 제출은 편성 순서대로 합니다 — 코너 콜이 앞 코너의 마지막 멘트보다 먼저 나가지 않습니다.

### DJ persona

- persona 파일: `config/personas/*.yaml` — 프리셋 `saebyeok`(심야·차분한 존댓말, 기본), `haessal`(아침·경쾌한 존댓말), `dodo`(주말·반말).
- 필드: `dj_name`, `tone`, `concept` (필수) + `examples`(대표 멘트 3~5개), `forbidden`, `signature_phrases` (선택). 설계: [docs/persona.md](../../docs/persona.md)
- 말투를 바꾸고 싶으면 `tone` 형용사보다 `examples`를 고칩니다. 예시는 데이터 블록으로 들어가며, LLM은 말투·호흡만 따르고 문장은 베끼지 않도록 지시받습니다.
- 실행 시 바꿔 끼우기: `onair-engine --persona config/personas/dodo.yaml ...`

### 대본 품질 평가 (`onair-eval`)

persona × 고정 시나리오(`config/eval_cases.yaml` — 오프닝·곡 소개·브리핑·사연 답변·브릿지, 인젝션·개인정보·비방 사연 포함)로 엔진과 같은 생성 파이프라인을 돌리고 지표를 냅니다. TTS는 쓰지 않습니다.

```powershell
# persona 3종 전부, 시나리오 2회 반복
python -m onair_engine.eval --llm openai --llm-base-url https://api.groq.com/openai/v1 `
  --llm-model openai/gpt-oss-120b --llm-reasoning-effort low --repeat 2
# 특정 persona만 + LLM-judge 채점 (judge 모델을 생성 모델과 다르게 두는 편이 낫다)
python -m onair_engine.eval --persona config/personas/dodo.yaml --llm claude --judge --judge-model claude-sonnet-5-5
```

- 채팅 인용만 볼 때는 `--cases config/eval_cases_chat.yaml`을 씁니다. 짧은 채팅 20개(주제에 맞는 말, 주제 밖의 말, DJ에게 묻는 말, 주제를 묻는 말, 짧은 반응, 읽으면 안 되는 요청)를 오늘의 이야기와 곡 소개에 번갈아 넣고, 리포트에 채팅 유형 × 단계별 인용 누락 표와 누락된 대본을 붙입니다. 채팅 인용 프롬프트(`corners/steps.py`의 `QUOTE_RULE`)나 인용 검사(`pipeline.quoted`)를 고칠 때 먼저 돌립니다.
- 한 멘트에 입력 여러 건을 싣는 멘트는 `--cases config/eval_cases_multi.yaml`로 봅니다. 단계 이름 뒤의 `+chat2`는 채팅 두 건을, `story+chat`은 사연과 채팅 한 건을 한 멘트에 넣습니다. 두 건을 넣은 멘트는 한 건이라도 빠지면 누락으로 세고, 건마다의 판정은 `results.jsonl`의 `quotes`에 있습니다. 이 멘트들은 엔진이 계약 v2로 보낼 때만 방송에 나옵니다(#267).
- 결과: `var/eval/<시각>/report.md` (요약표, 코너별 발화 시간, 경고 붙은 멘트, 전체 대본), `results.jsonl` (멘트 단위 원자료), `run.json` (재현 정보 — 커밋, LLM, 사례 파일), `worktree.diff` (커밋하지 않은 변경이 있을 때)
- 프롬프트를 고치기 전·후의 결과는 `onair-pairwise`로 견줍니다 (아래 "튜닝 도구")
- 자동 지표: L1 거부·L2 차단·대체 횟수, 사연 판정 오류(과잉 거부/거부 누락), 금지 문자열 노출(인젝션 성공·개인정보), 인용 누락(넣으라고 준 채팅이 대본에 없다), 말투 이탈 문장(존댓말/반말 종결 어미 — 프롬프트의 예시를 따라 쓴 말끝은 잡지 못합니다), 길이·문장 수 초과, 기호·영어·숫자 누출, 상투어, 입버릇·이름 언급 빈도, 직전 멘트 유사도·첫마디 중복, 지연
- `--judge`: 캐릭터(설정·예시와 같은 사람인가)·자연스러움·과제 수행을 1~5점으로 매기고, 캐릭터 2점 이하 비율을 **캐릭터 이탈률**(persona.md 7절)로 냅니다.
- 자동 지표는 의심 신호입니다. 경고가 붙은 멘트와 전체 대본을 직접 읽고 판단하세요. 청취자 입력단 L0는 거치지 않습니다(L1·L2를 직접 시험하기 위해).

### 방송 지표 리포트 (`onair-report`)

방송이 끝난 뒤 계측 DB(`telemetry.sqlite_path`)에서 방송 1회의 지표를 뽑습니다. 읽기 전용입니다.

```powershell
python -m onair_engine.report                          # 방송 ID와 회차 수
python -m onair_engine.report st_local_dev             # 마지막 회차 요약 (마크다운 표)
python -m onair_engine.report st_local_dev --run 1     # 첫 회차. 음수는 뒤에서부터
python -m onair_engine.report st_local_dev --json      # 원자료
```

- 지표
  - 입력: 종류·경로별 건수, 요청 최종 상태
  - 반영: 유형별 반영률, 제때 반영·화제 일치 비율, 반영까지 걸린 시간 중앙값·P95
  - 코너: 진입·건너뜀·대체 횟수, 길이
  - 생성: 단계별·세그먼트별 지연, 대체 멘트·거부·미완료, 인용 검사 건수와 누락률. 누락된 건은 채팅·대본·일치 비율을 같이 보여 줍니다(계측 DB의 `quote_log`) — 채팅을 건너뛴 것인지, 인용했는데 검사가 못 알아본 것인지는 대본을 읽어야 갈립니다
  - 그 밖: 사연 판정, 화제 전환, 집계 언급·예고
  - 멘트: 같은 첫머리로 시작한 멘트 비율, 직전 멘트와의 유사도, 주제 소개 횟수
- **멘트 지표**: 송출된 대본(`aired_log`)으로 셉니다. 고정 문구는 대본 기록이 없어 빠집니다.
  - 같은 첫머리: 공백·문장부호를 뺀 앞 5글자가 앞선 멘트와 같은 비율. "겨울 간식은 …"으로 계속 시작하면 올라갑니다
  - 유사도: 멘트마다 직전 8개 중 가장 비슷한 것과의 글자 3-gram 자카드 유사도 평균(0~1)
  - 주제 소개: "오늘은 ○○ 이야기를 …"처럼 주제를 소개한 멘트 수. 오프닝의 1번이 정상입니다
  - 1차 시나리오 실행 기준으로 같은 첫머리는 quiet 36~81%, 청취자가 있는 시나리오 8~31%였습니다
- **회차**: 백엔드는 같은 방송 ID를 다시 쓰므로 `station.created`(없으면 `station.started`)마다 회차를 나눕니다. 그 기록이 없는 예전 DB는 전체가 한 회차입니다.
- **코너 길이**: 그 코너가 낸 멘트 길이입니다. 곡 길이는 넣지 않습니다. 코너 예산(`budget_sec`)도 같은 값으로 잽니다. 이 기록이 없는 예전 DB에서는 그 코너를 편성하는 동안 제출한 오디오 길이를 쓰며, 앞 코너에서 소개한 곡이 섞여 길게 나옵니다.
- **건너뜀 횟수**: 소재를 기다리는 동안 같은 코너의 건너뜀은 한 번만 기록됩니다.

### 시나리오 반복 실행 (`onair-scenario`)

같은 조건의 방송을 여러 번 돌려 문제를 찾고, 고친 뒤 전후를 비교합니다. 엔진은 실제 LLM으로 **실시간** 실행하고, 백엔드 송출과 TTS는 흉내 냅니다. 엔진의 인용 간격·후보 창·방송 종료가 실제 시계로 돌기 때문에 시간을 압축하지 않습니다.

```powershell
# 1차 시나리오 5개 × 5회, 동시 10회 (기본값: 방송 30분, LLM gpt-6-luna)
python -m onair_engine.scenario config/scenarios/quiet.yaml config/scenarios/sparse.yaml `
  config/scenarios/demo.yaml config/scenarios/flood.yaml config/scenarios/adversarial.yaml
python -m onair_engine.scenario config/scenarios/demo.yaml --llm dummy --minutes 8 --repeat 1  # 도구 점검, 무료
python -m onair_engine.scenario config/scenarios/sparse.yaml --persona config/personas/dodo.yaml  # 페르소나 비교
python -m onair_engine.scenario config/scenarios/demo.yaml --tts gemini --repeat 1 --yes  # 실제 TTS
# 설정값 비교 — 지금 값(base)과 30·120을 같은 seed로 함께 돌린다. 더미 LLM이라 무료
python -m onair_engine.scenario config/scenarios/sparse.yaml config/scenarios/flood.yaml --llm dummy `
  --sweep standalone_window_sec=300,900 --repeat 10 --parallel 60 --note "독립형 창 비교"
```

- **흉내 내는 것**
  - 백엔드: 실제 길이만큼 재생하고 PUBLISHED·`segment.started`(방송 지연 포함)를 보낸다. 남은 분량을 보고하고, 다음 곡과 신청곡을 알린다
  - TTS: 글자 수에 비례하는 무음. 지연은 2026-11-02 Gemini 실측(오디오 4초 → 약 7초, 25초 → 약 14초)을 잇는 식이다
  - LLM(`--llm dummy`일 때): 단계별 고정 대본. 지연은 2026-11-07·10 실측(gpt-6-luna low, 호출 456건: 평균 1.5~1.6초, 표준편차 0.4초)에 맞춥니다. 더미는 넣으라는 채팅을 항상 인용하므로 반영률이 실제보다 높게 나옵니다 — 같은 실측에서 채팅 인용 38건 중 19건이 빠졌습니다. 이 누락을 흉내 내려면 `--set sim.llm_quote_miss_rate=0.5`를 씁니다
  - Redis, HLS 플레이어 지연, 실제 발음은 흉내 내지 않습니다
- **시나리오 파일** (`config/scenarios/*.yaml`)
  - 방송 설정, 흉내 값(`sim`), 시각별 입력(`inputs`), 배경 채팅(`chatter`), 게시판 사연을 적습니다
  - 게시판 사연은 `id`를 붙여 `expect`에서 가리킬 수 있고, `at`을 적으면 방송 중에 올라온 글로 보냅니다
  - 운영자 조작(`controls`)은 테스트 화면의 버튼입니다. 코너 버튼(`{at, corner}`), 페르소나 버튼(`{at, type: persona_switch, persona: <persona 파일>}`), 스테이션 버튼(`{at, type: format_switch, format, corners?, news_sections?}`)이 있습니다. 가짜 백엔드가 실제 백엔드처럼 재생 중인 것과 미리 인코딩한 분량(`sim.buffer_low_sec`)을 뺀 멘트를 버리고 엔진에 알립니다
    - `demo_buttons.yaml`이 데모의 코너 버튼 구간, `demo_persona.yaml`이 페르소나 버튼 구간, `demo_format.yaml`이 스테이션 버튼 구간입니다
    - 기사(`news: [{title, summary, section}]`)를 적으면 실제 피드를 읽지 않고 그 기사를 씁니다. 읽는 데 `sim.rss_latency_sec`(기본 2초)가 걸리는 것으로 흉내 냅니다. 적지 않은 뉴스 시나리오는 실제 피드를 읽습니다
    - 페르소나 버튼의 보이스는 `--tts gemini`일 때만 씁니다(`voice`, 없으면 방송의 보이스). persona 파일에 `style`이 없으면 기본값을 채워 보냅니다
  - 기대 결과(`expect`)를 적으면 회차마다 통과 여부를 판정합니다: `reflected`, `min_reflected`(입력 종류 `kind`별 반영 건수 하한), `aired_contains`, `not_aired`, `max_silence_sec`, `max_fallback`, `corner_entered`(버튼을 누른 뒤 고른 코너의 첫 멘트가 들리기까지, 방송 지연 포함), `persona_switched`(페르소나 버튼을 누른 뒤 새 DJ의 첫 멘트가 들리기까지, 방송 지연 포함. 엔진이 거절하면 실패), `format_switched`(스테이션 버튼을 누른 뒤 새 형식의 첫 멘트가 들리기까지, 같은 기준), `not_reflected`(그 입력이 방송에 반영되지 않았는가)
  - `flood`는 채팅 폭주에 사연 보내기, 신청곡, 게시판 사연(주제 관련·무관, 방송 전·방송 중)을 함께 넣어 종류별 선별을 봅니다. 건수 기준은 2026-11-12 실행 20회에서 잡았습니다(`docs/ENGINE_TUNING_LOG.md` T-009)
    - 무관한 사연이 걸러지는지와 신청자 닉네임을 부르는지는 실제 LLM에서만 판정됩니다. 더미 LLM은 사연을 모두 관련 있다고 보고 고정 대본을 씁니다
    - 엔딩 곡으로 나간 신청곡은 반영 건수에 잡히지 않을 수 있습니다. 엔진이 끝난 뒤의 송출은 기록만 하기 때문입니다
  - 신청곡은 실제 백엔드처럼 새 신청이 다음 곡이 되고, 밀린 신청은 최근 것부터 나갑니다. 엔진이 소개를 이미 만들고 있던 곡은 밀려나도 그대로 나가고, 다시 틀지 않습니다
  - `config/scenarios/holdout/`은 검증용입니다. 설정값이나 프롬프트를 고칠 때는 돌리지 않고, 채택하려는 변경이 다른 주제·입력에서도 통하는지 볼 때만 돌립니다
- **설정값 비교** (`--set`, `--sweep`, `--variant`): 코드를 고치지 않고 값을 바꿔 돌립니다
  - 이름은 `config/chat_rules.yaml`의 항목(`flood_threshold`), 엔진 설정(`engine.target_buffer_sec`, `engine.max_concurrent_generations`, `engine.contract_version`), 시나리오의 방송 설정(`station.intensity`), 흉내 값(`sim.llm_quote_miss_rate`)입니다. `engine.contract_version=2`는 한 멘트에 입력 여러 건을 싣는 동작을 켭니다 — 흉내 백엔드는 두 버전을 모두 받습니다. 틀린 이름과 범위 밖 값은 실행 전에 거절합니다
  - `--set 이름=값`은 모든 회차에, `--sweep 이름=값,값`은 값마다 변형을 하나씩, `--variant 이름:설정=값,설정=값`은 여러 값을 함께 바꾼 변형을 만듭니다. 지금 설정은 `base`라는 변형으로 항상 같이 돕니다
  - 변형이 달라도 같은 회차 번호는 같은 seed입니다(입력 도착 시각, TTS·LLM 흉내 지연, 더미 대본이 같습니다). 그래서 회차끼리 짝지어 차이를 봅니다
  - 선별 기준, 인용 창(`reaction_window_sec`), 집계 언급은 사용자 실험의 조건입니다(CORNER_DESIGN 10절). 값을 훑어보는 것은 괜찮지만 한 값으로 굳히지 않습니다
- **출력** (`var/scenario/<실행 시각>/<시나리오>/` — 변형이 여럿이면 `<실행 시각>/<변형>/<시나리오>/`)
  - `run-N/transcript.md`: 송출 시각 순 대본. 코너, 들어온 입력, 반영된 입력, 무음, 곡을 함께 보여 줍니다
  - `run-N/report.md`: `onair-report` 지표, 무음, 말과 곡의 흐름, 기대 결과
    - 말 비율: 말 시간 ÷ (말 + 곡)
    - 곡 사이 토크: 곡과 곡 사이 멘트 길이의 중앙값. 첫 곡 전 오프닝과 마지막 곡 뒤 엔딩은 넣지 않습니다
    - 곡 연속: 곡 소개만 사이에 두고 곡이 이어진 횟수
    - 코너 변경: 버튼마다 버린 멘트 수, 그대로 나간 분량, 고른 코너 첫 멘트의 제출·재생·청취까지 걸린 시간 (`report.json`의 `sim.corner_skips`)
    - 페르소나 전환: 버튼마다 버린 멘트 수, 이전 DJ의 목소리로 그대로 나간 분량, 새 DJ 첫 멘트의 제출·재생·청취까지 걸린 시간, 엔진이 받아들였는지 (`report.json`의 `sim.persona_switches`)
    - 형식 전환: 버튼마다 새 형식의 첫 코너, 버린 멘트 수, 이전 형식으로 그대로 나간 분량, 첫 멘트의 제출·재생·청취까지 걸린 시간 (`report.json`의 `sim.format_switches`)
  - `run-N/run.json`: 재현 정보 — 시나리오(덮어쓴 흉내 값 포함), seed, git 커밋과 커밋하지 않은 변경 여부, 변형과 덮어쓴 설정, LLM. 규칙을 덮어썼으면 그 회차가 쓴 `chat_rules.yaml`도 같이 남습니다
  - `summary.md`: 회차별 핵심 지표, 기대 결과 통과 회차
  - `issues.csv`: 빈 문제 기록표
  - `--tts gemini`: 회차마다 멘트 오디오 재생 목록 `broadcast.m3u` (곡은 없습니다)
  - `review.html`: 대본 검토 페이지 (아래)
  - `session.json`: 실행 한 번의 기록 — 명령, 커밋, 변형, 목적(`--note`). 커밋하지 않은 엔진 변경이 있으면 `worktree.diff`로 같이 남깁니다
  - `compare.md`, `compare.json`: 변형이 여럿일 때 `base` 대비 지표 비교 (아래 "튜닝 도구")
  - `var/scenario/ledger.jsonl`: 실행할 때마다 변형 × 시나리오마다 한 줄(지표 평균·표준편차, 커밋, 설정)을 덧붙이는 장부
- **대본 검토 페이지** (`onair-review`, 관리자용): 실행기는 무음·반영·기대 결과처럼 형식만 판정합니다. 같은 말 반복, 흐름 단절, 청취자 글 오해, 지어낸 사실처럼 읽어야 아는 문제는 사람이 이 페이지에서 잡습니다
  - 실행이 끝나면 `<실행 시각>/review.html`이 만들어집니다. 서버 없이 브라우저로 엽니다
  - 수정 전·후처럼 여러 실행을 한 페이지에 모으려면: `onair-review var/scenario/<전> var/scenario/<후> --label 수정전 --label 수정후 -o review.html`. `played.json`이 없는 예전 회차는 `transcript.md`를 읽습니다
  - **검토**: 멘트를 누르고 분류(반복, 흐름 단절, 청취자 오해, 지어냄, 전환 어색, 질문 남발, 길이, 소리·발음, 좋음)와 메모를 답니다. j/k로 이동, 1~9로 분류. 같은 첫머리, 직전 8개와 비슷한 멘트, 질문으로 끝나는 멘트 3번 연속, 주제 다시 소개는 자동 표시로 먼저 짚습니다(판정이 아니라 볼 곳). 곡마다 직전 곡 뒤 말 길이를 보여 주고 15초 미만이면 강조합니다
  - **비교**: 블라인드로 같은 시나리오에서 출처가 다른 두 회차를 무작위로 나란히 보여 줍니다. 더 자연스러운 쪽을 고르면 직전 쌍을 공개하고 다음 쌍으로 넘어갑니다. 지표가 좋아져도 귀로는 나빠졌을 수 있는 변경(말이 짧아지는 변경 등)을 판단할 때 씁니다
  - 표시는 그 브라우저(localStorage)에만 저장됩니다. **표시 CSV**(issues.csv 형식, 같은 시나리오의 재현 회차 수 포함)와 **비교 CSV**로 내보내고, 다른 사람과는 **백업**·**불러오기**로 나눕니다
- **비용**: 실제 LLM을 쓰면 시작 전에 예상 호출 수를 보여 주고 확인을 받습니다(`--yes`로 생략). 30분 방송 1회의 LLM 비용은 약 1~2센트로 추정합니다(2026-11-07 실측: 호출당 입력 약 630~930, 출력 약 130토큰)
- **한계**: 회차가 실시간으로 돌기 때문에 30분 방송은 30분 걸립니다. 동시 실행 수(`--parallel`)를 늘리면 전체 시간이 줄어듭니다. 엔진이 끝난 뒤 대기열에 남은 송출은 기다리지 않고 기록만 합니다. 그래서 `--minutes`로 짧게 줄인 방송은 도구 점검에만 씁니다 — `--minutes 6`은 곡 두 개로 분량이 차서 1분쯤에 끝납니다
- **동시 실행**: 모든 회차가 한 프로세스의 이벤트 루프에서 돕니다. 루프가 밀리면 엔진의 타이머가 늦게 깨어나 생성 시간과 반영 시간이 길게 재집니다
  - 회차의 계측 DB는 디스크 동기화 없이 쓰고(동기화를 켠 채 동시 30회를 돌리면 루프 지연 P95가 1.7초였습니다), 회차를 `--stagger`초(기본 1초) 간격으로 띄웁니다
  - 회차마다 루프 지연을 재서 `report.json`의 `sim.loop_lag_ms_p95`와 요약 표에 남깁니다. 250ms를 넘으면 실행 끝에 경고합니다 — `--parallel`을 줄여 다시 돌립니다
  - 2026-11-10 실측: 더미 LLM으로 flood 30분 방송을 동시 60회 돌리면 루프 지연 P95가 180~200ms이고, 같은 seed의 세그먼트 생성 P95는 동시 4회와 같았습니다(12.21초, 12.22초). 입력이 많은 시나리오는 60회를 넘기지 않습니다
  - 실제 LLM 실행은 한 번에 하나만, 동시 35회 이하로 돌립니다. 2026-11-11에 두 실행을 동시 70회씩 겹쳐 돌리자 시작 뒤 5분 동안 LLM 호출의 절반이 5초를 넘었고, 그 묶음의 세그먼트 생성 P95가 2.4~4.2초 길게 재졌습니다(루프 지연 경고는 뜨지 않았습니다). 동시 35회 단독 실행은 루프 지연 P95 62ms 이하였습니다 (docs/ENGINE_TUNING_LOG.md T-004)
  - 실제 TTS는 Gemini 한도가 동시 실행 수를 정합니다

### 튜닝 도구 (`onair-compare`, `onair-pairwise`)

설정값·프롬프트·코드를 바꿀 때의 절차와 판정 기준은 [docs/ENGINE_TUNING.md](../../docs/ENGINE_TUNING.md)에, 실험 기록은 [docs/ENGINE_TUNING_LOG.md](../../docs/ENGINE_TUNING_LOG.md)에 있습니다. 여기는 명령만 적습니다.

```powershell
python -m onair_engine.scenario.compare var/scenario/<시각>                 # 회차 간 편차 — 무엇이 노이즈인가
python -m onair_engine.scenario.compare var/scenario/<전> var/scenario/<후>   # 수정 전·후
python -m onair_engine.scenario.compare var/scenario/<--sweep 실행>           # base 대비 변형마다

python -m onair_engine.eval.pairwise var/eval/<전> var/eval/<후>              # 가려진 쌍 만들기
python -m onair_engine.eval.pairwise --tally var/pairwise/<시각>              # votes.csv를 채운 뒤 집계
python -m onair_engine.eval.pairwise var/eval/<전> var/eval/<후> --judge-llm claude --judge-model claude-sonnet-5-5
python -m onair_engine.eval.pairwise --blocks var/scenario/<전> var/scenario/<후> --limit 140 --judge-llm ...  # 곡 사이 말 전체끼리
python -m onair_engine.scenario.compare var/scenario/<전> var/scenario/<후> --refresh    # 실행 뒤에 생긴 지표를 채우고 비교
```

- **`onair-compare`**: 같은 회차 번호(같은 seed)끼리 짝지어 지표 차이를 봅니다
  - 차이의 평균이 회차 간 흔들림을 넘을 때만 개선·악화로 적습니다(짝 t 검정, 양쪽 95%, 3쌍 이상). 좋아지는 방향이 없는 지표(말 비율, 화제 전환 수)는 참고로만 나옵니다
  - 폴더 하나만 주면 지표별 평균·표준편차와 "구분되는 차이"(같은 회차 수로 두 조건을 견줄 때 이만큼은 달라져야 우연과 구분된다)를 보여 줍니다. 회차 수는 이 값을 보고 정합니다
  - `-o`로 마크다운을, `--json`으로 원자료를 파일로 씁니다. 표는 실험 기록에 그대로 붙일 수 있게 출처(폴더, 커밋, LLM, 덮어쓴 설정)를 머리에 적습니다
- **`onair-pairwise`**: 같은 persona·회차·단계의 대본 두 개를 어느 쪽이 고친 뒤인지 가린 채 보여 주고 나은 쪽을 고르게 합니다
  - `--blocks`는 `onair-scenario` 실행 폴더 두 개를 받아, 같은 시나리오·회차·자리의 곡과 곡 사이 말 전체를 견줍니다. 말이 이어지는지는 멘트 하나로는 볼 수 없습니다. `--limit`으로 시나리오마다 고르게 뽑습니다
- **`onair-compare --refresh`**: 실행 뒤에 추가된 지표(예: 고정 문구 "돌아왔어요" 횟수, 질문으로 끝난 멘트)를 회차의 `metrics.sqlite`에서 계산해 `report.json`에 채웁니다. 이미 있는 값은 건드리지 않습니다
  - `pairs.md`(읽을 것), `votes.csv`(가 / 나 / 같음을 적을 것), `key.json`(어느 쪽이 고친 뒤인지 — 고르기 전에 열지 않습니다)을 만듭니다. `--tally`가 선택을 되돌려 세고 `report.md`를 씁니다
  - 판정은 같음을 뺀 선택이 한쪽으로 치우쳤는지 봅니다(부호 검정, p < 0.05, 6쌍 이상). 먼저 보인 쪽을 고른 비율도 같이 나옵니다 — 50%에서 멀면 심사가 순서에 끌린 것입니다
  - `--judge-llm`을 주면 LLM이 고릅니다. 생성에 쓴 모델과 다른 모델을 씁니다. 사람이 고른 결과와 어긋나는지 가끔 확인합니다
  - 쌍 비교는 자연스러움만 봅니다. 길이 초과·말투 이탈 같은 규칙 위반은 `report.md`의 자동 지표 표(전·후)로 따로 확인합니다

### 오디오 파일 관리

- 모든 TTS 출력은 `audio.CachedTts`를 거칩니다.
  - **원자적 쓰기**: `.이름.랜덤.tmp.확장자`로 쓴 뒤 교체합니다. 백엔드는 완성된 파일만 봅니다.
  - **캐시**: `pipeline.tts_cache_dir`(기본 `var/tts_cache`)에 텍스트 단위로 저장합니다. 접착 문구·filler처럼 반복되는 문장은 API를 다시 호출하지 않습니다.
  - **길이 실측**: `duration_ms`는 파일에서 잰 값입니다 (mutagen).
- 파일 정리 책임과 보존 기간은 [Redis 계약 문서](../../docs/ENGINE_REDIS_CONTRACT.md) 5.1절에 있습니다.

### Redis 전송

계약: [docs/ENGINE_REDIS_CONTRACT.md](../../docs/ENGINE_REDIS_CONTRACT.md)

```powershell
pip install -e ".[redis]"
wsl sudo apt install -y redis-server   # 로컬 Redis (최초 1회)
wsl redis-server --daemonize yes
onair-engine --transport redis
wsl redis-cli XRANGE engine:st_local_dev:out - + COUNT 5
```

- 세그먼트 제출과 요청 상태는 `engine:{station_id}:out`으로 나갑니다.
- 백엔드가 `engine:{station_id}:in`에 넣은 `request.arrived`는 요청 큐로, `station.closed`는 방송 정지로 처리됩니다.
- 테스트는 fakeredis를 쓰므로 Redis 서버 없이 `pytest`로 돌아갑니다.

#### 운영 모드 (`--serve`) — 호스트가 만든 방 띄우기

```powershell
onair-engine --transport redis --serve
```

- `engine:control`의 `station.created`마다 방을 하나씩 띄웁니다 (설정 파일의 `station`은 쓰지 않음). 페이로드 검증은 [packages/onair_schema](../../packages/onair_schema/) `StationCreated`로 합니다.
- 검증·형식 설정·보이스 확인·접착 요소 사전 렌더링을 통과하면 `station.started`(확정 주제), 아니면 `station.rejected`(`reason`, `detail`)를 그 방의 `:out`으로 보냅니다. 넣어 보는 명령은 계약 문서 3.3절에 있습니다.
- `pipeline.tts: gemini`(또는 `cartesia`)면 `persona.voice`가 그 제공자의 보이스 목록에 있는지 확인합니다.
- 받은 `station.created` 원문은 계측 SQLite `station_log`에 persona 사본으로 남습니다.
- 엔진 REST(아래)도 함께 열립니다.

### 엔진 REST — 방 생성 화면용 API

계약: [docs/ENGINE_REDIS_CONTRACT.md](../../docs/ENGINE_REDIS_CONTRACT.md) 6장. `127.0.0.1:8100`에만 열고, `/health`를 뺀 `/v1`은 `Authorization: Bearer {ONAIR_ENGINE_TOKEN}`이 필요합니다(토큰을 안 정하면 인증 없이 열립니다).

```powershell
onair-engine --api                                  # REST만 (Redis 불필요 — 방 생성 화면 개발용)
onair-engine --api --llm openai --llm-model gpt-6-luna   # 실제 LLM으로 초안 만들기
```

| endpoint | 하는 일 |
|---|---|
| `POST /v1/personas/drafts` | 폼(`PersonaForm`) → persona 초안 N개. 기본 말투는 폼 선택지에서 규칙으로, 이름·컨셉·예시 멘트 등은 LLM이 만든다. 스키마·안전(L2)·말투 이탈 검사에서 떨어진 초안만 최대 2회 다시 만든다 |
| `POST /v1/personas/check` | 호스트가 고친 persona — 스키마·L0·보이스 검사. 실패하면 `422 {"errors": [{"field", "reason"}]}` |
| `GET /v1/voices` | 폼의 보이스 선택지 — Gemini 보이스 중 한국어로 표시된 것. `?native_only=false`면 전체 |
| `GET /v1/engine/status` | 떠 있는 방, 진행 중 생성 수, 최근 10분 생성 지연 P95 |
| `GET /v1/stations/{id}/decisions?after=&limit=` | 결정 로그 페이지 (`next`를 다음 `after`로) |
| `GET /v1/stations/{id}/events?after=&limit=` | 엔진 패널 이벤트 — 화면에 쓸 `summary`와 구조화 `data` (`event_log`) |
| `GET /health` | 프로세스 생존 (인증 없음) |

- **더미 LLM(`pipeline.llm: dummy`)도 초안을 돌려줍니다.** API 키 없이 프론트·백엔드가 방 생성 흐름을 개발할 수 있습니다.
- 실측 (2026-10-09, `gpt-6-luna`): 초안 3개 12.8~14.8초. 응답 목표는 15초 `[예시]`, 상한은 `api.draft_timeout_sec`(30초)입니다.
- API 문서: 엔진을 띄운 뒤 `http://127.0.0.1:8100/v1/docs`

## 테스트 / 린트

```bash
pytest
ruff check .
```

## 구조 (설계 문서 8장과 대응)

```
src/onair_engine/
├── main.py           # 엔트리포인트 (설정 로드 → run_local)
├── manager.py        # EngineManager — 다중 스테이션 수명주기, 스테이션 이벤트 라우팅
├── engine.py         # StationEngine — 스테이션 1개의 컴포넌트 조립
├── domain.py         # 데이터 모델 (제출 페이로드 — 계약 대상, 판단 기록)
├── format/           # 형식·코너·단계 정의(spec), config/formats 로더, 클락
├── corners/          # 단계별 프롬프트 빌더(steps), 코너 진행 상태(run), persona 시스템 프롬프트(base)
├── scheduler/        # 편성 관리자 — 버퍼 관리, 순서 보장 제출, 과도기 인용
├── pipeline/         # 프롬프트→LLM(L1)→인용 검사→L2→TTS→패키징, 더미 어댑터 포함
├── glue.py           # 접착 요소(코너 콜·마무리) 사전 렌더 — 기존 ack 캐시 대체
├── catalog.py        # 로컬 개발·테스트용 더미 곡 목록 (곡 선정은 백엔드 — track.queued)
├── sources/rss.py    # RSS 수집기 — 분야별 주기 폴링, 메모리 캐시
├── transport.py      # 백엔드 어댑터 (stdout 더미 / redis는 계약 확정 후)
└── telemetry.py      # SQLite 계측 (generation_log, request_log, decision_log, corner_log)
```

## 다음 단계

설계 문서 9장 단계별 구현 계획(M3~M6)과 10장 확인 필요 사항을 참고하세요.
제공자는 확정됐습니다(확인 3) — LLM `gpt-6-luna`(openai), TTS Gemini(2026-10-30 변경). `generation_log`의 llm/tts 단계 지연은 버퍼 기준값(F-31) 실측에 씁니다.
