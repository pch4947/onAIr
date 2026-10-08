# packages

백엔드와 대본 엔진이 같이 쓰는 공용 패키지.

## onair_schema

persona와 `station.created` 페이로드의 Pydantic v2 모델 (docs/persona.md 2.3절, docs/ENGINE_REDIS_CONTRACT.md 3.3절).
백엔드 방 생성 API·DB와 엔진이 같은 정의로 검증한다.

```bash
pip install -e packages/onair_schema          # 저장소 루트 기준. 엔진·백엔드 venv 각각에 설치
pytest packages/onair_schema                  # pip install -e "packages/onair_schema[dev]"
```

```python
from onair_schema import Persona, StationCreated

created = StationCreated.model_validate(payload)   # 실패하면 pydantic.ValidationError
```

- 필드 제한 수치는 `[예시]`다. 바꿀 때는 계약 문서 3.3절 표와 함께 바꾼다.
- 이미 구현된 필드의 의미를 바꾸면 계약의 `contract_version`을 올린다.
