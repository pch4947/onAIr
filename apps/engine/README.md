# onAIr 대본 엔진 (`apps/engine`)

편성 관리자가 코너 편성표에 따라 LLM 대본을 생성하고 TTS 오디오를 백엔드에 제출하는 파트입니다.

- 설계 문서: [docs/ENGINE_ARCHITECTURE.md](../../docs/ENGINE_ARCHITECTURE.md)
- 현재 상태: **M0 스켈레톤** — 더미 LLM/TTS로 파이프라인 종단 관통. 백엔드 미연동(stdout 더미 전송).

## 시작하기

```bash
cd apps/engine
python -m venv .venv
.venv\Scripts\activate        # Windows (bash: source .venv/Scripts/activate)
pip install -e ".[dev]"
```

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
2. 키를 환경변수로 넣고 실행합니다 (키는 설정 파일·커밋에 넣지 않습니다).

```powershell
$env:GOOGLE_TTS_API_KEY = "발급받은 키"
onair-engine --tts google --max-segments 15 --demo-request "요즘 잠이 안 와요"
```

- 기본 보이스는 `ko-KR-Chirp3-HD-Aoede`(여성)입니다. `pipeline.tts_voice`로 바꿉니다 (남성: `ko-KR-Chirp3-HD-Charon`).
- 추가 의존성은 없습니다 (표준 라이브러리 REST 호출).
- 다른 제공자(유료 모델, 로컬 오픈소스 TTS)로 옮길 때는 `pipeline/tts.py`에 `TtsClient` 프로토콜(`file_ext`, `cache_namespace`, `synthesize`)을 지키는 어댑터를 추가하고 `make_tts`에 등록하면 됩니다.

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
당장의 미결: 백엔드 통신 채널(확인 1, 1주차 계약), LLM/TTS 제공자 선정(확인 3).
