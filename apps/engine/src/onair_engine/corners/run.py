"""코너 1회 진행 — 단계 상태 머신 (CORNER_DESIGN 7.0절) + 항목 출처.

CornerRun은 "다음에 할 단계와 그 소재"만 정한다. 어떤 청취자 입력을 넣을지(인용)와
생성·제출은 편성 관리자(scheduler)의 몫이다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..catalog import Catalog
from ..format.clock import ClockEntry
from ..format.spec import CornerSpec, Intensity, StepSpec
from ..inputs.inbox import MessageInbox
from ..inputs.stories import StoryPool
from ..sources.rss import RssCollector, source_name
from .steps import STEP_KINDS

if TYPE_CHECKING:  # topic_plan → pipeline.llm → corners.steps 순환을 피한다
    from ..domain import BlockScript
    from ..inputs.topic_plan import Angle


@dataclass
class Thread:
    """화제 상태 — 코너 안에서 단계 사이에 넘긴다 (CORNER_DESIGN 5.1.1절).

    keywords는 화제 관련성 계산(M4 QuoteSelector, 키워드 겹침 — U1)에 쓴다.
    angle은 이번 진입에 쓰는 이야기 갈래다(inputs/topic_plan.py). 채팅이 화제를 바꾸면 사라진다.
    꺼내기 단계가 갈래의 말할 거리 하나를 쓰고, 이야기 단계가 points_left번까지 하나씩 더 꺼낸다.
    남은 말할 거리는 다음 진입이 잇는다(TopicPlan이 어디까지 썼는지 안다).
    갈래가 없으면 주제만으로 free_points번 이야기한다 — 소재가 떨어지면 단계를 끝낸다 (CORNER_DESIGN 7.7절).
    """

    topic: str | None = None
    keywords: list[str] = field(default_factory=list)
    angle: Angle | None = None
    points_left: int = 2
    free_points: int = 1


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

    def put_back(self, track: dict) -> None:
        """소개 멘트가 나가지 못한 곡을 되돌린다 (#216). 그 사이 알려 온 새 곡이 있으면 그것을 쓴다.

        되돌리지 않으면 곡이 시작되지 않아 백엔드도 다음 곡을 보내지 않는다 — 방송 끝까지 음악이 끊긴다.
        """
        if self._queued is None:
            self._queued = dict(track)

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
        # 이야기 단계의 말할 거리 — 채팅 풀·다시 꺼낼 이야기·갈래를 아는 편성 관리자가 채운다. 없으면 이야기 단계는 건너뛴다
        self.talk_points: Callable[[CornerRun], dict | None] | None = None

    def use_rss(self, rss: RssCollector) -> None:
        """방송 중에 형식이 바뀌어 뉴스 소재가 바뀐다 (#260). 이미 전한 기사 기록은 그대로 둔다."""
        self._rss = rss

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
        if source == "talk_point":  # 진입 조건으로 쓰지 않는다 — 코너 안에서 단계마다 정한다
            return 0
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
                 thread: Thread, again: bool = False, continues: bool = False, after_song: bool = False):
        self.spec = entry.corner
        self.label = entry.label
        self.thread = thread
        self.again = again  # 같은 방송에서 이 코너에 이미 들어온 적이 있다 — 단계의 again을 쓴다
        # 지난번 말 코너도 이 코너였다 — 사이에 곡만 있었다. "다시 …로 돌아왔어요"를 틀지 않는다 (#262)
        self.continues = continues
        self.after_song = after_song  # 곡이 끝나고 들어온 코너 — 꺼내는 멘트가 곡 뒤를 잇는다
        self.block: BlockScript | None = None  # 이번 진입을 한 번에 쓴 이야기 덩어리
        self.block_next = 0  # 다음에 낼 문단 번호
        # 이 코너가 낸 멘트 길이 — 예산 기준 (#220). 방송 누적량으로 재면 앞 코너에서 소개한 곡이
        # 이 코너가 편성된 뒤에 제출되면서 곡 길이가 이 코너 몫으로 잡힌다
        self.spent_ms = 0
        self.pending = 0  # 생성 중인 세그먼트 — 0이 되고 closed면 코너 길이가 확정된다
        self.closed = False  # 남은 단계가 없다 (corner_log exit)
        self.step_index = 0
        self.steers = 0  # 코너당 화제 전환 횟수 (참여 강도 steer_limit)
        self.aggregated = False  # 집계 언급은 코너당 1회 (CORNER_DESIGN 9.2절)
        self.called_back = False  # 청취자 이야기 다시 꺼내기도 코너당 1회 (CORNER_DESIGN 7.7절)
        self._sources = sources
        self._intensity = intensity
        self._done_in_step = 0

    def next_step(self) -> PlannedStep | None:
        """다음 단계. None이면 코너 종료."""
        over_budget = self.spent_ms / 1000 >= self.spec.budget_sec[1]
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
                items = self._take(step)
                if not items:
                    self._advance()
                    continue
            self._done_in_step += 1
            return PlannedStep(self.spec, step.on_reentry() if self.again else step, items, self.label)
        return None

    def _take(self, step: StepSpec) -> list[dict]:
        if step.items != "talk_point":
            return self._sources.take(step.items, step.take, self.label)
        point = self._sources.talk_points(self) if self._sources.talk_points else None
        return [point] if point else []

    def can_steer(self, limit: int) -> bool:
        """횟수 상한 미만이고, 바뀐 화제로 이야기할 단계와 그 시간이 남았다 (CORNER_DESIGN 5.2절)."""
        if self.steers >= limit:
            return False
        step = self._next_topic_step()
        if step is None:
            return False
        left_sec = self.spec.budget_sec[1] - self.spent_ms / 1000
        return left_sec >= STEP_KINDS[step.kind].seconds[0]

    def _next_topic_step(self) -> StepSpec | None:
        """남은 단계 중 화제로 이야기하는 첫 단계 — 항목이 소재인 단계는 화제를 쓰지 않는다.

        이야기 단계(talk_point)는 화제를 쓴다. 화제가 바뀌면 그 화제로 말할 거리를 새로 받는다(Thread.free_points).
        """
        for i, step in enumerate(self.spec.steps[self.step_index:]):
            left = self._repeat(step) - (self._done_in_step if i == 0 else 0)
            if left > 0 and step.items in (None, "talk_point") and step.kind != "glue":
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
