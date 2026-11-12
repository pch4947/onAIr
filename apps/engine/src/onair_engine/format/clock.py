"""클락 — 방송 시간을 코너 배치로 펼친다 (CORNER_DESIGN 6.2절, ENGINE_ARCHITECTURE 4.3절).

오프닝 1회 → 형식의 1회분 순환 틀을 반복 → 남은 방송 시간이 엔딩 길이에 닿으면 엔딩.
방송 길이를 맞추는 기준은 엔딩 시작 시각이다. 남은 시간은 스케줄러가 "지금까지 낸 오디오가
끝나는 시각" 기준으로 알려준다 — 엔진은 실제 재생보다 앞서 만들기 때문이다.
"""
from __future__ import annotations

import itertools
from collections import deque
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
        self._ahead: deque[ClockEntry] = deque()  # upcoming()이 미리 꺼낸 코너
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

    def upcoming(self, n: int) -> list[ClockEntry]:
        """순환에서 다음에 올 코너 n개 — 진입 조건으로 건너뛸 수 있으므로 예상일 뿐이다."""
        while len(self._ahead) < n:
            self._ahead.append(next(self._cycle))
        return list(self._ahead)[:n]

    def next(self, remaining_sec: float) -> ClockEntry | None:
        """다음 코너. 엔딩까지 끝났으면 None."""
        if self._phase == "opening":
            self._phase = "cycle"
            return ClockEntry(self.fmt.corner(self.fmt.opening))
        if self._phase == "cycle":
            if remaining_sec > self.closing_sec():
                return self._ahead.popleft() if self._ahead else next(self._cycle)
            self._phase = "closing"
        if self._phase == "closing":
            self._phase = "done"
            return ClockEntry(self.fmt.corner(self.fmt.closing))
        return None

    def jump(self, name: str) -> ClockEntry | None:
        """순환 차례와 상관없이 지금 들어갈 코너 (corner.skip). 순환은 그대로라 그 뒤는 원래 차례로 이어진다.

        오프닝·엔딩으로는 가지 않고, 오프닝 전이나 엔딩이 시작된 뒤에도 가지 않는다 — None.
        """
        if (self._phase != "cycle" or name not in self.fmt.corners
                or name in (self.fmt.opening, self.fmt.closing)):
            return None
        sectioned = f"$section:{name}" in self.fmt.cycle  # 분야마다 도는 코너는 첫 분야로
        return ClockEntry(self.fmt.corner(name), self._sections[0] if sectioned and self._sections else None)

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
