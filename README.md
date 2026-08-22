# onAIr

26-2 AI DJ 라이브 라디오 캡스톤 프로젝트입니다. 생성 지연이 가변적인 환경에서 공유 라이브 스트림의 연속성과 청취자 요청 반응성 사이의 상충을 다룹니다.

## 저장소 구조

```
onAIr/
|-- apps/
|   `-- frontend/
|-- packages/
|-- src/             # 초기 백엔드 서버
`-- docs/
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
npm start
```

기본 포트는 `3000`이며, 필요하면 `PORT` 환경 변수로 변경할 수 있습니다.

## 백엔드 API

```http
GET /health
GET /api/stream/state
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
