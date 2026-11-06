"""코너 1회 진행 — 단계 상태 머신 (CORNER_DESIGN 7.0절) + 항목 출처.

CornerRun은 "다음에 할 단계와 그 소재"만 정한다. 어떤 청취자 입력을 넣을지(인용)와
생성·제출은 편성 관리자(scheduler)의 몫이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..catalog import Catalog
from ..format.clock import ClockEntry
from ..format.spec import CornerSpec, Intensity, StepSpec
from ..inputs.inbox import MessageInbox
from ..inputs.stories import StoryPool
from ..sources.rss import RssCollector, source_name
from .steps import STEP_KINDS


@dataclass
class Thread:
    """화제 상태 — 코너 안에서 단계 사이에 넘긴다 (CORNER_DESIGN 5.1.1절).

    keywords는 화제 관련성 계산(M4 QuoteSelector, 키워드 겹침 — U1)에 쓴다.
    """

    topic: str | None = None
    keywords: list[str] = field(default_factory=list)


class TrackSource:
    """다음에 틀 곡 — 백엔드가 track.queued로 알려준 곡, 없으면 로컬 더미 목록 (계약 3.4절).

    더미 곡은 실제로 재생되지 않는다 — dummy 표시를 달아 music_ref와 방송 시간 계산에서 뺀다.
    백엔드는 다음 곡을 항상 하나 미리 정해 둔다. 엔진이 소개 멘트에 그 곡을 쓰면(take) 소비한 것이고,
    백엔드는 곡이 시작될 때 그다음 곡을 다시 보낸다. 그 전에 곡 소개 차례가 오면 더미 곡으로 메운다
    (백엔드가 track.queued를 보내기 전·로컬 실행 — #104).
    """

    def __init__(self, catalog: Catalog | None = None, *, dummy: bool = True):
        """dummy=False(운영)면 백엔드가 알려 준 곡만 쓴다 — 없으면 곡 소개 코너를 건너뛴다."""
        self._catalog = catalog or Catalog()
        self._dummy_enabled = dummy
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
        if not self._dummy_enabled:
            return None
        t = self._catalog.pick()
        if t is None:
            return None
        return {"track_ref": t.track_id, "title": t.title, "artist": t.artist,
                "duration_ms": t.duration_ms, "mood": t.mood, "dummy": True}


class MaterialSources:
    """단계가 꺼내 쓰는 항목의 출처 (spec.ItemSource).

    vote는 M5에서 채운다. 항목이 모자라면 그 항목이 필요한 코너는 진입 조건(requires)에서 걸러진다.
    """

    def __init__(self, *, tracks: TrackSource, rss: RssCollector,
                 inbox: MessageInbox | None = None, stories: StoryPool | None = None):
        self.tracks = tracks
        self._rss = rss
        self._aired_news: set[str] = set()
        self.covered_news: list[str] = []  # 이번 순환에서 전한 기사 제목 — recap 소재
        # `or`로 받지 않는다 — 빈 inbox·풀은 길이가 0이라 거짓이어서 넘겨받은 설정이 사라진다
        self.inbox = MessageInbox() if inbox is None else inbox
        self.stories = StoryPool() if stories is None else stories
        self.votes: list[dict] = []

    def count(self, source: str, section: str | None = None) -> int:
        if source in ("track", "next_track"):
            return 1 if self.tracks.peek() else 0
        if source == "news":
            return len(self._fresh_news(50, section))
        if source == "covered_news":  # 정리(recap) 코너의 진입 조건 — 이번 순환에 전한 기사가 있어야 한다
            return len(self.covered_news)
        if source == "message":
            return self.inbox.count()
        if source == "board_story":
            return self.stories.count()
        return len(self.votes)

    def take(self, source: str, n: int, section: str | None = None) -> list[dict]:
        if source == "track":
            return [t] if (t := self.tracks.take()) else []
        if source == "next_track":
            return [t] if (t := self.tracks.peek()) else []
        if source == "news":
            items = self._fresh_news(n, section)
            for it in items:
                self._aired_news.add(it["title"])
                self.covered_news.append(it["title"])
            return items
        if source == "message":
            return self.inbox.take(n)
        if source == "board_story":
            return self.stories.take(n)
        taken, self.votes[:] = self.votes[:n], self.votes[n:]
        return taken

    def _fresh_news(self, n: int, section: str | None = None) -> list[dict]:
        # 같은 방송에서 같은 기사를 두 번 전하지 않는다
        items = [it for it in self._rss.latest(50, section) if it.title not in self._aired_news]
        return [{"title": it.title, "summary": it.summary,
                 "source": it.source or source_name(it.source_url)}
                for it in items[:n]]

    def satisfies(self, corner: CornerSpec, section: str | None = None) -> bool:
        return all(self.count(src, section) >= n for src, n in corner.requires.items())


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
        self.steers = 0  # 코너당 화제 전환 횟수 (참여 강도 steer_limit)
        self.aggregated = False  # 집계 언급은 코너당 1회 (CORNER_DESIGN 9.2절)
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
                items = self._sources.take(step.items, step.take, self.label)
                if not items:
                    self._advance()
                    continue
            self._done_in_step += 1
            return PlannedStep(self.spec, step, items, self.label)
        return None

    def can_steer(self, limit: int, produced_ms: int) -> bool:
        """횟수 상한 미만이고, 바뀐 화제로 이야기할 단계와 그 시간이 남았다 (CORNER_DESIGN 5.2절)."""
        if self.steers >= limit:
            return False
        step = self._next_topic_step()
        if step is None:
            return False
        left_sec = self.spec.budget_sec[1] - (produced_ms - self.started_ms) / 1000
        return left_sec >= STEP_KINDS[step.kind].seconds[0]

    def _next_topic_step(self) -> StepSpec | None:
        """남은 단계 중 화제로 이야기하는 첫 단계 — 항목이 소재인 단계는 화제를 쓰지 않는다."""
        for i, step in enumerate(self.spec.steps[self.step_index:]):
            left = self._repeat(step) - (self._done_in_step if i == 0 else 0)
            if left > 0 and step.items is None and step.kind != "glue":
                return step
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
