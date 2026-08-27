"""Corner 인터페이스 — 편성 확장점 (설계 문서 4.3, 확인 2 결정).

기획서 4.2(코너 구성)가 확정되면 구현체를 교체/추가한다. 엔진 본체는 수정하지 않는다.
"""
from __future__ import annotations

from typing import ClassVar, Protocol

from ..domain import Material, Prompt, ScheduleContext, StationProfile

# L1 생성단 안전 지시 — 별도 LLM 호출 없이 대본 생성 프롬프트에 내장한다 (설계 문서 4.6)
L1_GUARD = (
    "소재가 방송에 부적합하면(개인정보 노출, 욕설·혐오, 특정인 비방, 위험 행위 조장) "
    "대본 대신 정확히 REJECT 한 단어만 출력한다."
)


def system_prompt(profile: StationProfile) -> str:
    return (
        f"당신은 라디오 스테이션 [{profile.concept}]의 DJ {profile.dj_name}다. "
        f"말투는 {profile.tone}. 대본 텍스트만 출력한다. {L1_GUARD}"
    )


class Corner(Protocol):
    corner_type: ClassVar[str]
    expected_duration_sec: ClassVar[tuple[int, int]]  # 편성 관리자의 시간 계산용

    async def gather(self, ctx: ScheduleContext) -> Material | None:
        """소재 수집. None이면 이번 차례를 진행할 수 없음 — 스케줄러가 filler로 대체한다."""
        ...

    def build_prompt(self, material: Material, profile: StationProfile) -> Prompt: ...
