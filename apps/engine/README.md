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
