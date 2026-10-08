"""공용 스키마 — 계약 3.3절 필드 표의 제한이 그대로 걸리는지 확인한다."""
import copy

import pytest
from onair_schema import Persona, StationCreated, error_detail
from pydantic import ValidationError

PAYLOAD = {
    "broadcast_minutes": 60,
    "first_song": {"track_ref": "yt:abc123", "title": "밤편지", "artist": "아이유",
                   "duration_ms": 253000},
    "topic": "시험 기간을 버티는 나만의 방법",
    "persona": {
        "persona_id": "psn_7c1e",
        "style": {"formality": "polite", "energy": "low", "humor": "rare"},
        "music_taste": ["발라드", "어쿠스틱"],
        "voice": "voice-ko-1",
        "dj_name": "새벽",
        "concept": "심야 스터디 라디오",
        "tone": "차분하고 따뜻한 존댓말. 말수가 적고 문장이 짧다",
        "examples": ["새벽 한 시가 넘었네요.", "오늘 분량을 다 못 끝내도 괜찮아요.",
                     "졸리면 물 한 잔이요."],
        "forbidden": ["큰 소리로 감탄하기"],
        "signature_phrases": ["천천히 가요"],
    },
    "policy": "naive_fifo",
}


def _with(path: str, value):
    payload = copy.deepcopy(PAYLOAD)
    *parents, key = path.split(".")
    target = payload
    for p in parents:
        target = target[p]
    if value is _DELETE:
        del target[key]
    else:
        target[key] = value
    return payload


_DELETE = object()


def test_contract_example_is_valid():
    created = StationCreated.model_validate(PAYLOAD)
    assert created.persona.dj_name == "새벽"
    assert created.first_song.duration_ms == 253000


def test_optional_fields_have_defaults():
    payload = _with("first_song", _DELETE)
    for key in ("topic", "policy"):
        del payload[key]
    for key in ("music_taste", "forbidden", "signature_phrases"):
        del payload["persona"][key]
    created = StationCreated.model_validate(payload)
    assert created.first_song is None and created.topic is None
    assert created.policy == "naive_fifo"
    assert created.persona.forbidden == []


@pytest.mark.parametrize("path, value, loc", [
    ("broadcast_minutes", 90, "broadcast_minutes"),
    ("persona.style.formality", "rude", "persona.style.formality"),
    ("persona.examples", ["하나", "둘"], "persona.examples"),
    ("persona.examples", "문자열 하나", "persona.examples"),  # 한 글자씩 쪼개지면 안 된다
    ("persona.dj_name", "   ", "persona.dj_name"),
    ("persona.dj_name", "가" * 21, "persona.dj_name"),
    ("persona.forbidden", ["금지"] * 11, "persona.forbidden"),
    ("persona.voice", _DELETE, "persona.voice"),
    ("persona.mood", "unknown", "persona.mood"),  # 모르는 필드 거부
    ("policy", "ack_then_defer", "policy"),
])
def test_contract_limits_are_enforced(path, value, loc):
    with pytest.raises(ValidationError) as exc:
        StationCreated.model_validate(_with(path, value))
    assert loc in error_detail(exc.value)


def test_station_needs_saved_persona_but_draft_does_not():
    draft = _with("persona.persona_id", _DELETE)["persona"]
    assert Persona.model_validate(draft).persona_id is None  # 초안 단계에는 ID가 없다
    with pytest.raises(ValidationError, match="persona_id"):
        StationCreated.model_validate(_with("persona.persona_id", _DELETE))


def test_strings_are_stripped():
    created = StationCreated.model_validate(_with("persona.dj_name", "  새벽  "))
    assert created.persona.dj_name == "새벽"


def test_error_detail_is_short():
    bad = _with("persona", {"style": {}})
    with pytest.raises(ValidationError) as exc:
        StationCreated.model_validate(bad)
    detail = error_detail(exc.value)
    assert detail.count(";") == 2 and "외" in detail
