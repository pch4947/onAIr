"""코너 1회 진행 — 단계 상태 머신 (CORNER_DESIGN 7.0절) + 항목 출처.

CornerRun은 "다음에 할 단계와 그 소재"만 정한다. 어떤 청취자 입력을 넣을지(인용)와
생성·제출은 편성 관리자(scheduler)의 몫이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..catalog import Catalog
from ..format.clock import ClockEntry
from ..format.spec import CornerSpec, Intensity, StepSpec
from ..sources.rss import RssCollector, source_name


@dataclass
class Thread:
    """화제 상태 — 코너 안에서 단계 사이에 넘긴다 (CORNER_DESIGN 5.1.1절).

    keywords는 화제 관련성 계산(M4 QuoteSelector, 키워드 겹침 — U1)에 쓴다.
    """

    topic: str | None = None
    keywords: list[str] = field(default_factory=list)


class TrackSource:
    """다음에 틀 곡 — 백엔드가 track.queued로 알려준 곡, 없으면 로컬 더미 목록 (계약 3.4절).

    백엔드는 다음 곡을 항상 하나 미리 정해 둔다. 엔진이 소개 멘트에 그 곡을 쓰면(take) 소비한 것이고,
    백엔드는 곡이 시작될 때 그다음 곡을 다시 보낸다. 그 전에 곡 소개 차례가 오면 더미 곡으로 메운다
    (백엔드가 track.queued를 보내기 전·로컬 실행 — #104).
    """

    def __init__(self, catalog: Catalog | None = None):
        self._catalog = catalog or Catalog()
        self._queued: dict | None = None
        self._peeked: dict | None = None  # artist_talk이 미리 본 더미 곡 — 같은 곡을 소개해야 한다

    def queue(self, track: dict) -> None:
        """track.queued — 새 곡이 이전 곡을 대체한다."""
        self._queued = dict(track)

    def peek(self) -> dict | None:
        if self._queued is not None:
            return self._queued
        if self._peeked is None:
            self._peeked = self._dummy()
        return self._peeked

    def take(self) -> dict | None:
        track = self.peek()
        self._queued = None
        self._peeked = None
        return track

    def _dummy(self) -> dict | None:
        t = self._catalog.pick()
        if t is None:
            return None
        return {"track_ref": t.track_id, "title": t.title, "artist": t.artist,
                "duration_ms": t.duration_ms, "mood": t.mood}


class MaterialSources:
    """단계가 꺼내 쓰는 항목의 출처 (spec.ItemSource).

    board_story·message는 M4(입력 라우팅), vote는 M5에서 채운다 — 그 전까지는 비어 있어서
    그 항목이 필요한 코너는 진입 조건(requires)에서 걸러진다.
    """

    def __init__(self, *, tracks: TrackSource, rss: RssCollector):
        self.tracks = tracks
        self._rss = rss
        self._aired_news: set[str] = set()
        self.covered_news: list[str] = []  # 이번 순환에서 전한 기사 제목 — recap 소재
        self.board_stories: list[dict] = []
        self.messages: list[dict] = []
        self.votes: list[dict] = []

    def count(self, source: str) -> int:
        if source in ("track", "next_track"):
            return 1 if self.tracks.peek() else 0
        if source == "news":
            return len(self._fresh_news(50))
        if source == "covered_news":  # 정리(recap) 코너의 진입 조건 — 이번 순환에 전한 기사가 있어야 한다
            return len(self.covered_news)
        return len(self._list(source))

    def take(self, source: str, n: int) -> list[dict]:
        if source == "track":
            return [t] if (t := self.tracks.take()) else []
        if source == "next_track":
            return [t] if (t := self.tracks.peek()) else []
        if source == "news":
            items = self._fresh_news(n)
            for it in items:
                self._aired_news.add(it["title"])
                self.covered_news.append(it["title"])
            return items
        pool = self._list(source)
        taken, pool[:] = pool[:n], pool[n:]
        return taken

    def _fresh_news(self, n: int) -> list[dict]:
        # 같은 방송에서 같은 기사를 두 번 전하지 않는다
        items = [it for it in self._rss.latest(50) if it.title not in self._aired_news]
        return [{"title": it.title, "summary": it.summary, "source": source_name(it.source_url)}
                for it in items[:n]]

    def _list(self, source: str) -> list[dict]:
        return {"board_story": self.board_stories, "message": self.messages,
                "vote": self.votes}[source]

    def satisfies(self, corner: CornerSpec) -> bool:
        return all(self.count(src) >= n for src, n in corner.requires.items())


@dataclass
class PlannedStep:
    corner: CornerSpec
    step: StepSpec
    items: list[dict]
    label: str | None


class CornerRun:
    """코너 1회. 단계를 차례로 내놓고, 항목이 떨어지거나 예산을 넘으면 그 단계를 끝낸다."""

    def __init__(self, entry: ClockEntry, *, sources: MaterialSources, intensity: Intensity,
                 thread: Thread, started_ms: int):
        self.spec = entry.corner
        self.label = entry.label
        self.thread = thread
        self.started_ms = started_ms  # 진입 시점의 누적 제출 오디오 — 예산 계산 기준
        self.step_index = 0
        self.steers = 0  # 코너당 화제 전환 횟수 (참여 강도 steer_limit) — M4
        self._sources = sources
        self._intensity = intensity
        self._done_in_step = 0

    def next_step(self, produced_ms: int) -> PlannedStep | None:
        """다음 단계. None이면 코너 종료."""
        over_budget = (produced_ms - self.started_ms) / 1000 >= self.spec.budget_sec[1]
        while self.step_index < len(self.spec.steps):
            step = self.spec.steps[self.step_index]
            # 예산을 넘었으면 남은 단계는 건너뛰되, 마무리 문구(glue close)와 곡 소개는 남긴다
            if over_budget and not _keeps_on_overrun(step):
                self._advance()
                continue
            if self._done_in_step >= self._repeat(step):
                self._advance()
                continue
            items: list[dict] = []
            if step.items is not None:
                items = self._sources.take(step.items, step.take)
                if not items:
                    self._advance()
                    continue
            self._done_in_step += 1
            return PlannedStep(self.spec, step, items, self.label)
        return None

    def _advance(self) -> None:
        self.step_index += 1
        self._done_in_step = 0

    def _repeat(self, step: StepSpec) -> int:
        if isinstance(step.repeat, int):
            return step.repeat
        return int(getattr(self._intensity, step.repeat))


def _keeps_on_overrun(step: StepSpec) -> bool:
    return step.glue == "close" or step.items == "track"
