# onAIr

26-2 AI DJ 라이브 라디오 캡스톤 프로젝트입니다. 생성 지연이 가변적인 환경에서 공유 라이브 스트림의 연속성과 청취자 요청 반응성 사이의 상충을 다룹니다.

## 저장소 구조

```
onAIr/
├── apps/
│   ├── backend/
│   ├── engine/
│   └── frontend/
├── packages/
└── docs/
```

## 시작하기

프론트엔드:

```bash
cd apps/frontend
npm install
cp .env.example .env
npm run dev
```

`.env`에 `VITE_API_BASE_URL`/`VITE_WS_URL`(백엔드)과 `VITE_HLS_URL`(HLS 스트림)을 채워주세요.

백엔드:

```bash
cd apps/backend
cp .env.example .env
npm start
```

기본 포트는 `3000`이며, 필요하면 `PORT` 환경 변수로 변경할 수 있습니다.

## 백엔드 API

```http
GET /health
GET /api/stream/state
GET /api/requests
POST /api/requests
POST /api/scheduler/tick
```

백엔드는 요청 큐, 생성 예상 시간, 남은 스트림 버퍼를 함께 보고 다음 편성 행동을 결정하는 초기 정책을 포함합니다. 자세한 구조는 [docs/architecture.md](docs/architecture.md)를 참고하세요.

## 커밋 전 체크

`apps/frontend` 안에서:

```bash
npm run lint
npm run typecheck
npm run build
```

브랜치, 커밋, PR 규칙은 [docs/CONVENTIONS.md](docs/CONVENTIONS.md)를 참고하세요.

## apps/engine

- 대본 엔진 (Python 3.12 + asyncio) — 편성 관리자, LLM/TTS 생성 파이프라인, 안전 계층
- 설계 문서: [docs/ENGINE_ARCHITECTURE.md](docs/ENGINE_ARCHITECTURE.md), 실행 방법: [apps/engine/README.md](apps/engine/README.md)

## apps/frontend

- 스택: Vite + React 19 + TypeScript + Tailwind CSS v4 (React Router, Zustand, TanStack Query, hls.js, idb, recharts는 의존성만 추가된 상태, 아직 미사용)
- 현재는 부트스트랩 뼈대(`app/App.tsx`, `main.tsx`, `index.css`)만 있고, 기능 폴더는 앞으로 채워나갈 자리로 비워뒀습니다:
  - `routes/{listen,experiment,admin}/` - 화면별 페이지 (예정)
  - `player/` - HLS 재생 훅 (예정)
  - `realtime/` - WebSocket 클라이언트 및 실시간 상태 스토어 (예정)
  - `interaction/` - 채팅/음성 요청 입력 (예정)
  - `telemetry/` - 지연 계측 (예정)
  - `experiment/` - 실험 그룹/설문 (예정)
  - `shared/` - 백엔드와 공유하는 타입 및 REST API 클라이언트 (예정)
- HLS 스트림(`VITE_HLS_URL`)과 REST/WS API(`VITE_API_BASE_URL`/`VITE_WS_URL`)는 서로 다른 서버에서 제공될 수 있으므로 환경변수를 분리해서 관리합니다.
