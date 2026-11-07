"""L0/L2 안전 규칙 (config/safety_rules.yaml) — 풀어 쓴 연락처와 일상 표현 오탐 (#218)."""
from pathlib import Path

import pytest

from onair_engine.pipeline.safety import SafetyChecker

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
safety = SafetyChecker(RULES)


@pytest.mark.parametrize("text", [
    "제 번호는 010-1234-5678 이에요",
    "https://example.com 들어와 보세요",
    "abc@example.com으로 보내 주세요",
    "제 블로그 놀러 오세요 example 닷컴",
    "네이버 점 컴에서 검색해 보세요",
    "example닷넷 많이 와 주세요",
    "example.com이에요",
    "블로그는 www.example.co.kr 입니다",
    "MY-SHOP.KR 놀러 오세요",
    "공일공 구팔칠육 오사삼이로 문자 주세요",
    "공일공-일이삼사-오육칠팔",
    "공일공 일이삼 사오육칠",
])
def test_contacts_are_blocked(text):
    assert safety.check_l0(text) is not None
    assert safety.check_l2(text) is not None


@pytest.mark.parametrize("text", [
    "오늘 비 오는데 잔잔한 곡 틀어주세요",
    "붕어빵의 장점 넷을 꼽아 볼게요",
    "닷새 뒤면 주말이에요",
    "날씨가 점점 추워지네요",
    "이 시점 컴백 소식도 있었죠",
    "점심에 컴퓨터 앞에서 호빵을 먹었어요",
    "이삼일 뒤에 다시 들려주세요",
    "오늘 별점 4.5점 드릴게요",
    "공일 하나 남았어요",
    "1.5배 빨리 지나간 하루였어요",
])
def test_everyday_speech_passes(text):
    assert safety.check_l0(text) is None
