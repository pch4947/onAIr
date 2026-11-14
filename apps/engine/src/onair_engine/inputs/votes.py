"""투표 라운드의 상태 — DJ 대 청취자 코너 (CORNER_DESIGN 7.6절, 계약 10.8절, #279).

라운드 하나: 고민과 선택지를 소개하는 멘트(기준 세그먼트)를 내고 → 그 멘트의 재생이 시작되면 백엔드가 투표 창을 열고 →
창이 닫히면 vote.result가 온다 → DJ의 선택과 결과를 공개한다. 엔진은 방송보다 앞서 만들기 때문에, 결과를 받기 전에는
공개 멘트를 만들 수 없다 — 그동안은 DJ 이야기로 잇는다.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..domain import new_id
from .dilemmas import Dilemma

MATCH, MISMATCH, TIE, NO_VOTES, LATE = "match", "mismatch", "tie", "no_votes", "late"
PENDING = object()  # 항목을 아직 낼 수 없다 — 투표 결과를 기다리는 중이다 (corners/run.py)
# 창이 열린 동안의 DJ 이야기가 방송에 나갈 때의 창 — 열려 있다, 곧 닫힌다(마감을 알린다), 닫혔다(결과를 기다린다)
OPEN, CLOSING, CLOSED = "open", "closing", "closed"


@dataclass
class VoteRound:
    vote_id: str
    dilemma: Dilemma
    segment_id: str | None = None  # 기준 세그먼트 — 제출하면 채워진다
    submitted_at: float | None = None
    opened_at: float | None = None  # 기준 세그먼트의 재생 시작 = 창이 열린 시각
    counts: dict[str, int] | None = None  # None이면 결과를 아직 받지 못했다
    talks: int = 0  # 창이 열린 동안 한 DJ 이야기 수
    phase: str = OPEN  # 가장 최근에 꺼낸 이야기가 나갈 때의 창
    closing_said: bool = False  # 마감을 알리는 멘트를 이미 꺼냈다

    @property
    def tally(self) -> tuple[int, int]:
        counts = self.counts or {}
        return counts.get(self.dilemma.options[0], 0), counts.get(self.dilemma.options[1], 0)

    @property
    def outcome(self) -> str:
        """DJ의 선택과 청취자 다수의 선택이 같은가. 결과를 못 받았으면 LATE."""
        if self.counts is None:
            return LATE
        first, second = self.tally
        if first + second == 0:
            return NO_VOTES
        if first == second:
            return TIE
        return MATCH if (0 if first > second else 1) == self.dilemma.pick else MISMATCH


def view(round_: VoteRound, score: dict[str, int]) -> dict:
    """프롬프트에 넣을 라운드의 지금 모습 (corners/steps.py의 game_* 단계)."""
    dilemma = round_.dilemma
    return {"question": dilemma.question, "options": list(dilemma.options), "pick": dilemma.pick,
            "reason": dilemma.reason, "talks": round_.talks, "phase": round_.phase, "outcome": round_.outcome,
            "tally": round_.tally, "score": dict(score)}


class VoteBoard:
    """방송 하나의 고민 목록과 진행 중인 라운드, 점수."""

    def __init__(self, *, window_sec: float = 60.0, grace_sec: float = 10.0, lead_sec: float = 120.0):
        self.window_sec = window_sec  # 투표 창 길이 — vote.open에 실어 보낸다
        self.grace_sec = grace_sec  # 창이 닫힌 뒤 결과를 더 기다리는 시간 (계약 10.8절 C4)
        # 기준 세그먼트의 재생 시작을 통보받지 못할 때, 제출 뒤 이만큼 지나면 열린 것으로 본다
        self.lead_sec = lead_sec
        # 지금 만드는 멘트가 방송에 나갈 시각의 추정 — 편성 관리자가 남은 분량으로 채운다. 엔진은 방송보다 앞서
        # 만들므로 "지금"이 아니라 이 시각으로 창이 열려 있는지를 본다
        self.queued_until = 0.0
        # 창이 닫히기 이만큼 전부터 나갈 멘트는 투표를 권하지 않고 마감을 알린다 — 그때 권해도 고를 시간이 없고,
        # 재생 시작 시각은 방송 지연만큼 어긋날 수 있다 [예시]
        self.closing_lead_sec = 25.0
        # 창이 닫힌 뒤 이만큼까지는 이야기로 잇는다 — 결과를 받아 공개 멘트를 만드는 시간이다 [예시]
        self.reveal_lead_sec = 15.0
        self._dilemmas: deque[Dilemma] = deque()
        self.active: VoteRound | None = None
        self.score = {"dj": 0, "listeners": 0, "draw": 0}  # DJ가 맞히면 DJ, 갈리면 청취자, 동률·표 없음은 무승부
        self.rounds = 0  # 결과까지 공개한 라운드 수

    def add(self, dilemmas: list[Dilemma]) -> None:
        self._dilemmas.extend(dilemmas)

    def count(self) -> int:
        """남은 고민 수 — 코너의 진입 조건(requires: vote)."""
        return len(self._dilemmas)

    def start(self) -> VoteRound | None:
        """다음 고민으로 라운드를 연다. 고민이 없으면 None."""
        if not self._dilemmas:
            return None
        self.active = VoteRound(new_id("vote"), self._dilemmas.popleft())
        return self.active

    def submitted(self, round_: VoteRound, segment_id: str, now: float) -> bool:
        """기준 세그먼트를 제출한다 — 그 사이 버려진 라운드면 False."""
        if round_ is not self.active:
            return False
        round_.segment_id, round_.submitted_at = segment_id, now
        return True

    def started(self, segment_id: str, started_at: float) -> None:
        if self.active is not None and self.active.segment_id == segment_id:
            self.active.opened_at = started_at

    def result(self, vote_id: str, counts: dict[str, int]) -> VoteRound | None:
        """vote.result — 진행 중인 라운드의 것만 받는다. 버린 투표·재전달·모르는 투표는 None."""
        round_ = self.active
        if round_ is None or round_.vote_id != vote_id or round_.counts is not None:
            return None
        round_.counts = dict(counts)
        return round_

    def waiting(self, now: float) -> bool:
        """결과를 기다리는 중인가 — 아직 받지 못했고 대기 상한도 지나지 않았다."""
        round_ = self.active
        if round_ is None or round_.counts is not None:
            return False
        if round_.segment_id is None:  # 고민 멘트를 아직 만들고 있다
            return True
        opened = round_.opened_at if round_.opened_at is not None else round_.submitted_at + self.lead_sec
        return now < opened + self.window_sec + self.grace_sec

    def next_talk(self, now: float) -> str | None:
        """결과를 기다리며 할 다음 이야기가 나갈 때의 창 (OPEN·CLOSING·CLOSED). 더 잇지 않아도 되면 None.

        이미 만들어 둔 분량이 결과 공개까지 이어지면 더 만들지 않는다 — 창이 닫힌 뒤에도 이야기가 이어져
        결과 공개가 늦어지지 않게 한다.
        """
        round_ = self.active
        if round_ is None or not self.waiting(now):
            return None
        if round_.opened_at is None:  # 고민 멘트가 아직 방송에 나가지 않았다 — 창은 열리기 전이다
            return OPEN
        closes = round_.opened_at + self.window_sec
        airs = max(now, self.queued_until)
        if airs >= closes + self.reveal_lead_sec:
            return None
        if airs < closes - self.closing_lead_sec:
            return OPEN
        return CLOSED if round_.closing_said else CLOSING

    def settle(self) -> VoteRound | None:
        """결과를 공개한다 — 점수를 매기고 라운드를 끝낸다."""
        round_, self.active = self.active, None
        if round_ is None:
            return None
        outcome = round_.outcome
        if outcome != LATE:
            self.rounds += 1
            self.score["dj" if outcome == MATCH else "listeners" if outcome == MISMATCH else "draw"] += 1
        return round_

    def abandon(self, *, requeue: bool = True) -> VoteRound | None:
        """코너가 끊겼다 — 고민 멘트를 아직 제출하지 않았으면 고민을 되돌린다. 열린 투표의 결과는 받지 않는다."""
        round_, self.active = self.active, None
        if round_ is not None and round_.segment_id is None and requeue:
            self._dilemmas.appendleft(round_.dilemma)
        return round_

    def discarded(self, segment_id: str) -> bool:
        """백엔드가 기준 세그먼트를 재생 전에 버렸다 — 투표는 열리지 않는다. 고민을 다음 차례로 되돌린다."""
        round_ = self.active
        if round_ is None or round_.segment_id != segment_id or round_.opened_at is not None:
            return False
        self._dilemmas.appendleft(round_.dilemma)
        self.active = None
        return True
