"""접착 요소 사전 렌더 (GlueBank) — 기존 ack 캐시 대체 (ENGINE_ARCHITECTURE 4.5절, CORNER_DESIGN 7.10절).

코너 콜·코너 마무리처럼 매번 같은 문구는 방송 시작 시 한 번 합성해 두고 재사용한다.
지연이 0이라 코너에 들어가자마자 내보내 버퍼 공백을 메운다.
문구는 형식 설정(config/formats)의 call/close 템플릿에 DJ 이름과 코너 이름을 채운 것이다.
persona 말투로 LLM이 한 번 다듬는 것은 이후 과제다 — 지금은 방송 시작 지연을 늘리지 않는다.
"""
from __future__ import annotations

from pathlib import Path

from .domain import SegmentKind, StationProfile, to_audio_ref
from .format.spec import FormatSpec
from .pipeline.safety import SafetyChecker
from .pipeline.tts import TtsClient

# 백엔드는 glue를 멘트와 같이 BGM 더킹하고, music_ref가 있으면 곡 소개로 다룬다 (#103)
WIRE_KIND = SegmentKind.GLUE


class GlueBank:
    def __init__(self, *, tts: TtsClient, audio_root: Path, station_id: str,
                 profile: StationProfile, fmt: FormatSpec, safety: SafetyChecker):
        self._tts = tts
        self._root = Path(audio_root)
        self._dir = self._root / station_id / "glue"
        self._profile = profile
        self._fmt = fmt
        self._safety = safety
        self._entries: dict[tuple[str, str], tuple[str, int]] = {}  # (코너, call|close) -> (audio_ref, ms)

    def texts(self) -> dict[tuple[str, str], str]:
        """이 형식에서 쓸 문구 전부. 문구가 없는 코너는 접착 요소 없이 바로 단계로 들어간다."""
        out = {}
        for name, corner in self._fmt.corners.items():
            for which in ("call", "close"):
                template = corner.glue_text(which)
                if template:
                    out[(name, which)] = template.format(title=corner.title,
                                                         dj=self._profile.dj_name)
        return out

    async def prerender(self) -> None:
        for i, ((corner, which), text) in enumerate(sorted(self.texts().items())):
            # DJ 이름은 호스트 입력이다 — 고정 문구라도 L2를 거친다
            if violation := self._safety.check_l2(text):
                raise ValueError(f"접착 문구가 안전 규칙에 걸림 ({corner}.{which}): {violation}")
            path = self._dir / f"{corner}_{which}_{i}{self._tts.file_ext}"
            duration_ms = await self._tts.synthesize(text, path)
            self._entries[(corner, which)] = (to_audio_ref(self._root, path), duration_ms)

    def get(self, corner: str, which: str) -> tuple[str, int] | None:
        return self._entries.get((corner, which))
