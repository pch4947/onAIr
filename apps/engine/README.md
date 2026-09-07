# onAIr 대본 엔진 (`apps/engine`)

편성 관리자가 코너 편성표에 따라 LLM 대본을 생성하고 TTS 오디오를 백엔드에 제출하는 파트입니다.

- 설계 문서: [docs/ENGINE_ARCHITECTURE.md](../../docs/ENGINE_ARCHITECTURE.md)
- 현재 상태: **M0 관통** — 더미 LLM/TTS가 공용 오디오 폴더에 파일을 만들고 HTTP로 백엔드에 제출.

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

- 백엔드가 먼저 `http://localhost:3000`에서 실행 중이어야 합니다.
- 세그먼트는 `POST /api/segments`로 제출됩니다.
- 생성된 오디오는 `../../var/onair-audio/`, 계측 로그는 `var/engine_metrics.sqlite`에 쌓입니다.
- `--max-segments` 없이 실행하면 설정된 방송 시간 동안 계속 돕니다 (Ctrl+C로 종료).

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
├── transport.py      # 백엔드 어댑터 (HTTP 제출 / stdout 더미)
└── telemetry.py      # SQLite 계측 (generation_log, request_log, decision_log)
```

## 다음 단계

설계 문서 9장 단계별 구현 계획(M0~M4)과 10장 확인 필요 사항을 참고하세요.
당장의 미결: LLM/TTS 제공자 선정(확인 3).
