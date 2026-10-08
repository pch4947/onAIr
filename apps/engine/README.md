# onAIr 대본 엔진 (`apps/engine`)

편성 관리자가 코너 편성표에 따라 LLM 대본을 생성하고 TTS 오디오를 백엔드에 제출하는 파트입니다.

- 설계 문서: [docs/ENGINE_ARCHITECTURE.md](../../docs/ENGINE_ARCHITECTURE.md)
- 현재 상태: **M1 진행 중** — 실 LLM(Claude/Gemini) → TTS(Google) → Redis 전송까지 종단 관통. 더미 어댑터는 키 없는 테스트·폴백용으로 유지.

## 시작하기

```bash
cd apps/engine
python -m venv .venv
.venv\Scripts\activate        # Windows (bash: source .venv/Scripts/activate)
pip install -e ".[dev]"
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
- `--max-segments` 없이 실행하면 설정된 방송 시간 동안 계속 돕니다 (Ctrl+C로 종료).
- 생성된 대본은 `SCRIPT seg_xxx [코너] 대본...` 라인으로 함께 출력됩니다.
- 요청 답변(request_reply)은 10번째 세그먼트 전후에 나오므로 요청 흐름을 보려면 `--max-segments 15` 이상을 주세요.

### 더미 대본을 실제 음성으로 듣기

더미 LLM은 코너별로 방송처럼 들리는 고정 대본을 반환합니다. `--tts edge`를 주면 Microsoft Edge 온라인 TTS(API 키 불필요, 인터넷 필요)가 대본을 한국어로 읽어 mp3로 저장합니다.

```bash
pip install -e ".[tts]"
onair-engine --tts edge --max-segments 15 --demo-request "요즘 잠이 안 와요"
```

- 오디오: `var/audio/st_local_dev/seg_xxx.mp3` (ack는 `ack/ack_N.mp3`)
- 보이스 변경: 설정 파일 `pipeline.tts_voice` (기본 `ko-KR-SunHiNeural`, 남성 `ko-KR-InJoonNeural`)
- 제공자 확정(확인 3) 전 청취 테스트용 어댑터입니다. 비공식 엔드포인트이므로 운영에는 쓰지 않습니다.

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

제공자는 비교 실측 중(확인 3)이라 두 어댑터를 같은 조건으로 붙여 두었습니다. 추가 의존성은 없습니다 (표준 라이브러리 REST 호출).

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
- **실패 처리**: LLM/TTS 호출은 1회 재시도하고, 재실패하면 더미의 고정 filler 대본으로 대체해 방송을 잇습니다. 요청 답변이 대체되면 요청을 `queued`로 한 번 되돌리고(ack는 다시 보내지 않음), 또 실패하면 `rejected`로 통보합니다. 계측 `generation_log`에 `error`/`fallback` 단계가 남습니다.

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

- 결과: `var/eval/<시각>/report.md` (요약표, 코너별 발화 시간, 경고 붙은 멘트, 전체 대본), `results.jsonl` (멘트 단위 원자료)
- 자동 지표: L1 거부·L2 차단·대체 횟수, 사연 판정 오류(과잉 거부/거부 누락), 금지 문자열 노출(인젝션 성공·개인정보), 말투 이탈 문장(존댓말/반말 종결 어미), 길이·문장 수 초과, 기호·영어·숫자 누출, 상투어, 입버릇·이름 언급 빈도, 직전 멘트 유사도·첫마디 중복, 지연
- `--judge`: 캐릭터(설정·예시와 같은 사람인가)·자연스러움·과제 수행을 1~5점으로 매기고, 캐릭터 2점 이하 비율을 **캐릭터 이탈률**(persona.md 7절)로 냅니다.
- 자동 지표는 의심 신호입니다. 경고가 붙은 멘트와 전체 대본을 직접 읽고 판단하세요. 청취자 입력단 L0는 거치지 않습니다(L1·L2를 직접 시험하기 위해).

### 오디오 파일 관리

- 모든 TTS 출력은 `audio.CachedTts`를 거칩니다.
  - **원자적 쓰기**: `.이름.랜덤.tmp.확장자`로 쓴 뒤 교체합니다. 백엔드는 완성된 파일만 봅니다.
  - **캐시**: `pipeline.tts_cache_dir`(기본 `var/tts_cache`)에 텍스트 단위로 저장합니다. ack·filler처럼 반복되는 문장은 API를 다시 호출하지 않습니다.
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

## 테스트 / 린트

```bash
pytest
ruff check .
```

## 구조 (설계 문서 8장과 대응)

```
src/onair_engine/
├── main.py           # 엔트리포인트 (설정 로드 → run_local)
├── manager.py        # EngineManager — 다중 스테이션 수명주기 (M3에서 이벤트 연동)
├── engine.py         # StationEngine — 스테이션 1개의 컴포넌트 조립
├── domain.py         # 데이터 모델 (제출 페이로드, 정책 입출력 등 — 계약 대상)
├── scheduler/        # 편성 관리자, 러닝오더, 정책(naive_fifo — A/B/C는 8장 확정 후)
├── corners/          # MVP 코너 5종 (opening/briefing/music_intro/request_reply/filler)
├── pipeline/         # 프롬프트→LLM(L1)→L2→TTS→패키징, 더미 어댑터 포함
├── ack.py            # 접수 확인 단축 경로 (사전 렌더 캐시)
├── catalog.py        # 음원 카탈로그 (엔진 소유)
├── sources/rss.py    # RSS 수집기 (M2 실구현)
├── transport.py      # 백엔드 어댑터 (stdout 더미 / redis는 계약 확정 후)
└── telemetry.py      # SQLite 계측 (generation_log, request_log, decision_log)
```

## 다음 단계

설계 문서 9장 단계별 구현 계획(M0~M4)과 10장 확인 필요 사항을 참고하세요.
당장의 미결: LLM/TTS 제공자 선정(확인 3) — `generation_log`의 llm/tts 단계 지연으로 비교 실측.
