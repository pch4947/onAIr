# onAIr
26-2 capstone

## 저장소 구조

```
onAIr/
├── apps/
│   └── frontend/     # Vite + React 19 + TypeScript + Tailwind CSS v4
├── packages/         # 공유 패키지 (현재 비어 있음, 추후 shared-types 등 추가 예정)
└── docs/
```

JS 패키지가 `apps/frontend` 하나뿐이라 pnpm workspace나 Turborepo 없이, `apps/frontend`를 일반 npm 프로젝트로 독립 실행합니다. 나중에 `packages/shared-types`처럼 JS 패키지가 늘어나면 그때 pnpm workspace + Turborepo 구성을 다시 고려합니다.

## 시작하기

```bash
cd apps/frontend
npm install
cp .env.example .env
npm run dev
```

`.env`에 `VITE_API_BASE_URL`/`VITE_WS_URL`(FastAPI 백엔드)과 `VITE_HLS_URL`(nginx가 서빙하는 HLS 스트림)을 채워주세요.

## 커밋 전 체크

`apps/frontend` 안에서:

```bash
npm run lint
npm run typecheck
npm run build
```

브랜치/커밋/PR 컨벤션은 [docs/CONVENTIONS.md](docs/CONVENTIONS.md)를 참고하세요.

## apps/frontend

- 스택: Vite + React 19 + TypeScript + Tailwind CSS v4, React Router, Zustand, TanStack Query, hls.js, idb, recharts
- 라우트: `/listen`(청취자 플레이어), `/experiment`(실험 참여), `/admin`(계측 대시보드) — `/`는 `/listen`으로 리다이렉트
- 폴더 구조(`src/` 기준):
  - `app/` — 앱 셸, 라우팅
  - `routes/` — 페이지별 화면
  - `player/` — HLS 재생 훅
  - `realtime/` — WebSocket 클라이언트 및 실시간 상태 스토어
  - `interaction/` — 채팅/음성 요청 입력
  - `telemetry/` — IndexedDB 기반 지연 계측(L_ack, L_res, E_sync)
  - `experiment/` — 실험 그룹/설문
  - `shared/` — 백엔드와 공유하는 타입(`types.ts`)과 REST API 클라이언트(`api.ts`)
- 백엔드 API 계약(요청 상태값, WebSocket 이벤트 타입 등)은 `src/shared/types.ts`에 정의되어 있고, REST 호출은 `src/shared/api.ts`를 통해서만 이루어집니다.
- HLS 스트림(`VITE_HLS_URL`, nginx)과 REST/WS API(`VITE_API_BASE_URL`/`VITE_WS_URL`, FastAPI)는 서로 다른 서버이므로 환경변수를 분리해서 관리합니다.
