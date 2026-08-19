# Git Convention

## Git Flow

```
main → dev → feat/chore/...
```

작업 흐름: issue 생성 → branch 생성 → commit → push → PR

## Commit 컨벤션

```
feat: 설명 (#이슈번호)
```

- 맨 뒤에 이슈번호 추가

## Branch 컨벤션

```
feat/#이슈번호-(설명간단히)
```

## PR 제목 컨벤션

```
[TYPE] 설명 (#이슈번호)
```

예시:
- `[FEAT] 회원가입 API 구현 (#14)`
- `[FIX] 이미지 업로드 시 NPE 수정 (#23)`
- `[REFACTOR] 토큰 로직 분리 (#8)`
- `[DOCS] ERD 스키마 업데이트 (#6)`
- `[CHORE] CI/CD 파이프라인 추가 (#3)`
- `[RELEASE] v1.0.0 배포 (#30)`

TYPE: `FEAT`, `FIX`, `DOCS`, `REFACTOR`, `TEST`, `CHORE`, `RENAME`, `REMOVE`, `RELEASE`
