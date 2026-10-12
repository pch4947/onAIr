"""클락 — 방송 시간을 코너 배치로 펼친다 (CORNER_DESIGN 6.2절, ENGINE_ARCHITECTURE 4.3절).

오프닝 1회 → 형식의 1회분 순환 틀을 반복 → 남은 방송 시간이 엔딩 길이에 닿으면 엔딩.
방송 길이를 맞추는 기준은 엔딩 시작 시각이다. 남은 시간은 스케줄러가 "지금까지 낸 오디오가
끝나는 시각" 기준으로 알려준다 — 엔진은 실제 재생보다 앞서 만들기 때문이다.
"""
from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass

from .spec import CornerSpec, FormatSpec


@dataclass(frozen=True)
class ClockEntry:
    corner: CornerSpec
    label: str | None = None  # 뉴스 분야처럼 같은 코너를 다른 소재로 돌릴 때의 이름


class Clock:
    def __init__(self, fmt: FormatSpec, *, main_corners: list[str] | None = None,
                 sections: list[str] | None = None):
        self.fmt = fmt
        self._main = list(main_corners or fmt.default_main or fmt.host_corners)
        unknown = [c for c in self._main if c not in fmt.host_corners]
        if unknown:
            raise ValueError(f"{fmt.format} 형식에서 고를 수 없는 코너: {unknown}")
        self._sections = [fmt.section_names.get(code, code)
                          for code in (sections or fmt.default_sections)]
        self._cycle = self._cycle_entries()
        self._phase = "opening"  # opening → cycle → closing → done

    @property
    def phase(self) -> str:
        return self._phase

    def closing_sec(self) -> float:
        """엔딩에 남겨 둘 시간 — 엔딩 코너 예산 상한 + 마지막 곡."""
        return self.fmt.corner(self.fmt.closing).budget_sec[1] + self.fmt.track_sec_estimate

    def main_titles(self) -> list[str]:
        """오프닝의 코너 예고에 쓴다."""
        return [self.fmt.corner(c).title for c in self._main]

    def next(self, remaining_sec: float) -> ClockEntry | None:
        """다음 코너. 엔딩까지 끝났으면 None."""
        if self._phase == "opening":
            self._phase = "cycle"
            return ClockEntry(self.fmt.corner(self.fmt.opening))
        if self._phase == "cycle":
            if remaining_sec > self.closing_sec():
                return next(self._cycle)
            self._phase = "closing"
        if self._phase == "closing":
            self._phase = "done"
            return ClockEntry(self.fmt.corner(self.fmt.closing))
        return None

    def _cycle_entries(self) -> Iterator[ClockEntry]:
        main = itertools.cycle(self._main) if self._main else None
        for name in itertools.cycle(self.fmt.cycle):
            if name == "$main":
                yield ClockEntry(self.fmt.corner(next(main)))
            elif name.startswith("$section:"):
                # $section:<코너> — 뉴스 분야마다 그 코너를 한 번씩
                corner = self.fmt.corner(name.split(":", 1)[1])
                for section in self._sections or [None]:
                    yield ClockEntry(corner, label=section)
            else:
                yield ClockEntry(self.fmt.corner(name))
