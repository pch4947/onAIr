"""TTS와 백엔드 송출 흉내 — 엔진은 그대로 두고 바깥만 바꿔 끼운다.

가짜 백엔드는 실제 백엔드(apps/backend/app/streaming/hls.py)가 엔진에 돌려주는 신호만 흉내 낸다.
- 세그먼트를 받은 순서대로 실제 길이만큼 "재생"한다. 곡 소개 멘트 뒤에는 그 곡을 곡 길이만큼 튼다
- 재생을 시작하면 PUBLISHED(송출 이력)와 segment.started(방송 지연을 더한 시각)를 보낸다
- 남은 분량(backpressure)을 상태가 바뀔 때나 10초마다 보낸다
- 다음 곡(track.queued)을 미리 알려 주고, 신청곡이 오면 다음 곡으로 올린다
- 테스트 화면의 버튼(코너 변경, 페르소나 전환, 형식 전환)을 누르면 아직 내보내지 않은 멘트를 버리고 엔진에 알린다
- 투표(vote.open)를 받으면 기준 세그먼트의 재생 시작에 창을 열고, 닫힐 때 가짜 표를 세어 vote.result를 보낸다
Redis·HLS·플레이어 지연은 흉내 내지 않는다.
"""
from __future__ import annotations

import asyncio
import random
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..domain import SegmentSubmission
from ..pipeline.llm import DummyLlmClient
from .spec import SimSpec, TrackSpec

_BACKPRESSURE_POLL_SEC = 2.0
_BACKPRESSURE_REPEAT_SEC = 10.0  # 백엔드와 같다 — 상태가 같아도 이 간격으로 다시 보낸다


class SimTts:
    """글자 수에 비례한 길이의 무음 wav. 지연은 Gemini 실측에 맞춘 식으로 낸다.

    파일 크기를 줄이려고 1kHz 8비트로 쓴다 — 길이만 맞으면 된다 (방송 시간 계산은 파일 길이를 잰다).
    """

    file_ext = ".wav"
    cache_namespace = "sim"
    RATE = 1000

    def __init__(self, sim: SimSpec, rng: random.Random):
        self.sim = sim
        self._rng = rng
        self.latencies: list[float] = []

    async def synthesize(self, text: str, out_path: Path) -> int:
        audio_sec = max(1.0, len(text) / self.sim.tts_chars_per_sec)
        jitter = self._rng.uniform(1 - self.sim.tts_jitter, 1 + self.sim.tts_jitter)
        latency = (self.sim.tts_latency_base_sec + self.sim.tts_latency_per_audio_sec * audio_sec) * jitter
        self.latencies.append(latency)
        await asyncio.sleep(latency)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)
            w.setframerate(self.RATE)
            w.writeframes(b"\x80" * int(self.RATE * audio_sec))
        return int(audio_sec * 1000)


def tune_dummy_llm(llm: object, sim: SimSpec, seed: int) -> list[float] | None:
    """엔진이 만든 더미 LLM에 실측에 맞춘 지연과 인용 누락을 넣는다. 실제 LLM이면 건드리지 않는다.

    더미 대본의 문장 고르기도 seed로 고정한다 — 같은 seed의 회차끼리 짝지어 비교하기 위해서다.
    돌려주는 목록에는 호출마다 낸 지연이 쌓인다.
    """
    if not isinstance(llm, DummyLlmClient):
        return None
    delays: list[float] = []
    rng = random.Random(seed * 15485863)

    def delay() -> float:
        delays.append(sim.llm_latency_sec * rng.uniform(1 - sim.llm_jitter, 1 + sim.llm_jitter))
        return delays[-1]

    llm.delay_sec = delay
    llm.quote_miss_rate = sim.llm_quote_miss_rate
    llm._rng = random.Random(seed * 32452843)
    return delays


class LoopLag:
    """실행기 이벤트 루프의 지연을 잰다 — 회차를 한 프로세스에서 동시에 돌리는 값이다.

    엔진의 인용 창·버퍼 판단·TTS 흉내는 모두 타이머로 돈다. 동시에 도는 회차가 많아 루프가 밀리면
    타이머가 늦게 깨어나고, 그만큼 생성 시간과 반영 시간이 길게 재진다. 그런 실행은 비교에 쓰면 안 된다.
    """

    INTERVAL_SEC = 0.5

    def __init__(self) -> None:
        self.samples_ms: list[float] = []

    async def watch(self, finished: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not finished.is_set():
            before = loop.time()
            await asyncio.sleep(self.INTERVAL_SEC)
            self.samples_ms.append(max(0.0, loop.time() - before - self.INTERVAL_SEC) * 1000)

    def percentile(self, pct: int) -> float | None:
        if not self.samples_ms:
            return None
        ordered = sorted(self.samples_ms)
        return round(ordered[max(0, -(-pct * len(ordered) // 100) - 1)], 1)


@dataclass
class Played:
    """송출 기록 한 줄 — 대본(transcript)의 원자료. at은 방송 시작 기준 초."""

    at: float
    kind: str  # speech | music | silence | input
    sec: float
    segment_id: str | None = None
    corner_type: str | None = None
    script: str | None = None
    text: str | None = None  # 곡 제목, 입력 내용
    audio_ref: str | None = None


@dataclass
class SimBackend:
    sim: SimSpec
    tracks: list[TrackSpec]
    rng: random.Random
    engine: object = None  # StationEngine — 엔진을 만든 뒤 넣는다
    played: list[Played] = field(default_factory=list)
    states: list[tuple[float, str, str]] = field(default_factory=list)
    stations: list[tuple[float, str, dict]] = field(default_factory=list)

    def __post_init__(self):
        self.started = asyncio.Event()  # station.started를 받았다
        self.finished = asyncio.Event()  # 엔진이 끝났다 — 실행기가 켠다
        self._cursor = 0.0  # 기록한 송출이 끝나는 시각 (방송 시작 기준 초)
        self.started_at: float | None = None
        self._queue: deque[SegmentSubmission] = deque()
        self._arrived = asyncio.Event()
        self._playlist = deque(self.tracks)
        self._by_ref: dict[str, TrackSpec] = {}
        self._queued_ref: str | None = None
        self._queued_request: dict | None = None  # 다음 곡이 신청곡이면 그 신청
        self._requests: list[dict] = []  # 다음 곡 자리에서 밀린 신청곡
        self._ref_requests: dict[str, dict] = {}  # 곡 ref → 신청 (소개가 버려진 신청곡을 다시 알릴 때)
        self._next_ref = 0
        self._current_end = 0.0  # 지금 재생 중인 것이 끝나는 시각(time.time 기준)
        self._last_pressure: tuple[str, float] | None = None
        self._payloads: dict[str, dict] = {}  # 곡 ref → 엔진에 알린 track.queued
        self.skips: list[dict] = []  # 코너 변경 기록 — 버튼부터 새 코너 첫 멘트의 제출·재생까지
        self.persona_switches: list[dict] = []  # 페르소나 전환 기록 — 버튼부터 새 DJ 첫 멘트의 제출·재생까지
        self.format_switches: list[dict] = []  # 형식 전환 기록 — 버튼부터 새 형식 첫 멘트의 제출·재생까지
        self.votes: list[dict] = []  # 투표 기록 — 받은 vote.open과 창을 연·닫은 시각, 보낸 결과
        self._vote_tasks: set[asyncio.Task] = set()

    @property
    def engine_done(self) -> bool:
        """엔진이 끝나 계측이 닫혔다 — 그 뒤로는 엔진에 알리지 않는다."""
        return self.finished.is_set()

    # ── Transport ────────────────────────────────────────────────────
    async def publish_segment(self, sub: SegmentSubmission) -> None:
        self._queue.append(sub)
        self._arrived.set()
        skip = self.skips[-1] if self.skips else None
        if skip and skip["segment_id"] is None and sub.corner_type == skip["corner"]:
            skip["segment_id"] = sub.id  # 고른 코너의 첫 세그먼트 (콜 문구일 수 있다)
            skip["submit_sec"] = round(self.now() - skip["at"], 1)
        switch = self.persona_switches[-1] if self.persona_switches else None
        # 엔진이 페르소나를 바꾼 뒤에 낸 첫 세그먼트 — 그 전에 만들던 것은 엔진이 버린다
        if (switch and switch["accepted"] and switch["segment_id"] is None
                and self.engine.config.persona_id == switch["persona_id"]):
            switch["segment_id"] = sub.id
            switch["submit_sec"] = round(self.now() - switch["at"], 1)
        change = self.format_switches[-1] if self.format_switches else None
        # 엔진이 형식을 바꾼 뒤에 낸 첫 세그먼트
        if (change and change["accepted"] and change["segment_id"] is None
                and getattr(self.engine.scheduler, "format_switches", 0) > change["applied_before"]):
            change["segment_id"] = sub.id
            change["corner"] = sub.corner_type
            change["submit_sec"] = round(self.now() - change["at"], 1)
        if sub.music_ref and sub.music_ref == self._queued_ref:
            self._queued_ref = self._queued_request = None
            self.queue_next_track()  # 소개할 곡을 엔진이 가져갔다 — 백엔드가 다음 곡을 미리 알린다
        elif sub.music_ref in self._by_ref:
            self._forget_displaced(sub.music_ref)

    async def notify_request_state(self, request_id: str, state, detail: dict | None = None) -> None:
        self.states.append((self.now(), request_id, str(state)))

    async def notify_station(self, event_type: str, payload: dict) -> None:
        self.stations.append((self.now(), event_type, payload))
        if event_type == "station.started":
            self.started_at = time.time()
            self.started.set()
        elif event_type == "vote.open":  # 기준 세그먼트의 재생이 시작될 때 연다 (계약 10.8절)
            self.votes.append({**payload, "at": round(self.now(), 1), "opened_at": None, "closed_at": None,
                               "counts": None, "discarded": False})

    # ── 곡 ───────────────────────────────────────────────────────────
    def queue_next_track(self, request: dict | None = None) -> None:
        """다음 곡을 엔진에 알린다. request는 신청곡 (TrackSpec, request_id, nickname).

        실제 백엔드(streaming/tracks.py)처럼 새 신청곡이 다음 곡이 되고, 밀린 신청곡은 남아 최근 것부터 나간다.
        """
        if request is None and self._requests:
            request = self._requests.pop()
        if request is not None:
            track = request["track"]
            self._put_back_queued()
        else:
            track = self._playlist.popleft()
            self._playlist.append(track)
        self._next_ref += 1
        ref = f"sim:{self._next_ref}"
        self._by_ref[ref] = track
        self._queued_ref = ref
        self._queued_request = request
        if request is not None:
            self._ref_requests[ref] = request
        payload = {"track": {"track_ref": ref, "title": track.title, "artist": track.artist,
                             "duration_ms": int(track.sec * 1000)}}
        if request is not None:
            payload.update(source="request", request_id=request["request_id"],
                           nickname=request.get("nickname"))
        self._payloads[ref] = payload
        self.engine.on_track_queued(payload)

    def _put_back_queued(self) -> None:
        """알려 둔 다음 곡을 다음 차례로 돌린다 — 신청곡은 밀린 신청으로, 플레이리스트 곡은 순서의 맨 앞으로."""
        if self._queued_request is not None:
            self._requests.append(self._queued_request)
        elif self._queued_ref is not None:
            self._playlist.rotate(1)  # queue_next_track이 맨 뒤로 보낸 곡이다

    def _forget_displaced(self, ref: str) -> None:
        """밀려난 곡의 소개가 왔다 — 엔진이 그 곡의 소개를 이미 만들고 있었다. 곡은 그대로 나가므로 다시 틀지 않는다."""
        request = self._ref_requests.get(ref)
        if request is not None:
            if request in self._requests:
                self._requests.remove(request)
        elif self._playlist and self._playlist[0] is self._by_ref[ref]:
            self._playlist.rotate(-1)

    # ── 코너 변경 ────────────────────────────────────────────────────
    async def skip_corner(self, corner: str, control_id: str | None = None) -> None:
        """테스트 화면의 코너 버튼 — 실제 백엔드(apps/backend/app/test_controls.py)가 하는 일을 흉내 낸다.

        아직 내보내지 않은 멘트를 버리고(segment.discarded) corner.skip을 보낸다.
        """
        record = {"id": control_id, "corner": corner, "at": round(self.now(), 1),
                  "segment_id": None, "submit_sec": None, "play_sec": None}
        self.skips.append(record)
        await self._discard_unsent(record)
        self.engine.on_corner_skip({"corner": corner})

    async def switch_persona(self, persona: dict, control_id: str | None = None) -> None:
        """테스트 화면의 페르소나 버튼 — 아직 내보내지 않은 멘트를 버리고 station.persona_changed를 보낸다.

        persona는 station.created의 persona와 같은 구조다. 엔진이 받아들였는지(accepted)도 기록한다.
        """
        record = {"id": control_id, "persona_id": persona.get("persona_id"), "dj_name": persona.get("dj_name"),
                  "at": round(self.now(), 1), "accepted": None,
                  "segment_id": None, "submit_sec": None, "play_sec": None}
        self.persona_switches.append(record)
        await self._discard_unsent(record)
        record["accepted"] = await self.engine.on_persona_changed({"persona": persona})

    async def switch_format(self, options: dict, control_id: str | None = None) -> None:
        """테스트 화면의 스테이션 버튼 — 아직 내보내지 않은 멘트를 버리고 station.format_changed를 보낸다.

        options는 {format, corners?, news_sections?}다. 실제 백엔드는 토크로 바뀌면 아직 보내지 않은 게시판
        사연도 보내는데, 여기서는 사연을 방송 시작 때(또는 적어 둔 시각에) 형식과 상관없이 보낸다.
        """
        record = {"id": control_id, "format": options["format"], "at": round(self.now(), 1), "accepted": None,
                  "applied_before": getattr(self.engine.scheduler, "format_switches", 0),
                  "segment_id": None, "corner": None, "submit_sec": None, "play_sec": None}
        self.format_switches.append(record)
        await self._discard_unsent(record)
        record["accepted"] = self.engine.on_format_changed(options)

    async def _discard_unsent(self, record: dict) -> None:
        """송출기가 아직 손대지 않은 멘트를 대기열에서 버리고 segment.discarded를 보낸다 (test_controls.py의 discard_and_send).

        재생 중인 것과 미리 인코딩해 둔 분량(buffer_low_sec)은 그대로 나간다 — 곡이 나가는 중이면 곡이 끝나야 바뀐다.
        버려진 곡 소개의 곡은 다음 곡으로 다시 알린다 (계약 3.2절 — 백엔드가 그 곡의 예약을 풀어야 한다).
        record에는 버린 멘트 수와 그대로 나가는 분량을 적는다.
        """
        ahead = max(0.0, self._current_end - time.time())
        kept: deque[SegmentSubmission] = deque()
        discarded: list[SegmentSubmission] = []
        for sub in self._queue:
            if ahead < self.sim.buffer_low_sec:
                kept.append(sub)
                ahead += sub.duration_ms / 1000 + (self._track_sec(sub) or 0)
            else:
                discarded.append(sub)
        self._queue = kept
        record.update(discarded=len(discarded), ahead_sec=round(ahead, 1))
        gone = {s.id for s in discarded}
        for vote in self.votes:  # 기준 세그먼트가 버려진 투표는 열지 않는다
            if vote["opened_at"] is None and vote["segment_id"] in gone:
                vote["discarded"] = True
        if discarded:
            await self.engine.on_segments_discarded({"segment_ids": [s.id for s in discarded]})
        intro = next((s for s in discarded if s.music_ref in self._payloads), None)
        if intro is not None:
            self._put_back_queued()  # 그 뒤에 알린 곡은 다음 차례로 돌린다 — 신청곡이면 신청을 잃지 않는다
            self._queued_ref = intro.music_ref
            self._queued_request = self._ref_requests.get(intro.music_ref)
            self.engine.on_track_queued(self._payloads[intro.music_ref])

    # ── 재생 ─────────────────────────────────────────────────────────
    def now(self) -> float:
        """방송 시작 기준 초 — 시작 전이면 음수가 아니라 0."""
        return max(0.0, time.time() - self.started_at) if self.started_at else 0.0

    def remaining_sec(self) -> float:
        """백엔드가 재는 남은 분량 — 재생 중인 것의 남은 길이 + 대기열(소개된 곡 포함)."""
        queued = sum(s.duration_ms / 1000 + (self._track_sec(s) or 0) for s in self._queue)
        return max(0.0, self._current_end - time.time()) + queued

    def _track_sec(self, sub: SegmentSubmission) -> float | None:
        track = self._by_ref.get(sub.music_ref) if sub.music_ref else None
        return track.sec if track else None

    async def play(self, engine_finished: asyncio.Event) -> None:
        """대기열을 실시간으로 재생한다. 엔진이 끝나고 대기열이 비면 멈춘다."""
        await self.started.wait()
        gap_from: float | None = None
        first = True
        while True:
            if not self._queue:
                if engine_finished.is_set():
                    break
                if not first and gap_from is None:
                    gap_from = time.time()  # 첫 세그먼트 전 준비 시간은 무음으로 세지 않는다
                self._arrived.clear()
                await self._wait_any(self._arrived, engine_finished)
                continue
            if gap_from is not None:
                self.played.append(Played(gap_from - self.started_at, "silence", time.time() - gap_from))
                gap_from = None
            first = False
            sub = self._queue.popleft()
            await self._play_segment(sub)

    async def _play_segment(self, sub: SegmentSubmission) -> None:
        sec = sub.duration_ms / 1000
        # 대본은 PUBLISHED를 보내기 전에 본다 — 엔진이 송출 이력으로 옮기며 지운다
        script = self.engine.scheduler.pipeline._pending_air.get(sub.id)
        self.played.append(Played(self._at(), "speech", sec, sub.id, sub.corner_type, script,
                                  audio_ref=sub.audio_ref))
        self._current_end = time.time() + sec
        for record in (*self.skips, *self.persona_switches, *self.format_switches):
            if record["segment_id"] == sub.id:
                record["play_sec"] = round(self.now() - record["at"], 1)
        for vote in self.votes:
            if vote["segment_id"] == sub.id and vote["opened_at"] is None and not vote["discarded"]:
                # 창은 청취자가 그 멘트를 듣기 시작할 때 연다 — segment.started의 started_at과 같은 시각이다
                vote["opened_at"] = round(self.now() + self.sim.broadcast_delay_sec, 1)
                self.played.append(Played(self._at() + self.sim.broadcast_delay_sec, "input", 0,
                                          text=f"투표 열림 {vote['vote_id']}: {' / '.join(vote['options'])}"))
                task = asyncio.create_task(self._close_vote(vote))
                self._vote_tasks.add(task)
                task.add_done_callback(self._vote_tasks.discard)
        if not self.engine_done:
            self.engine.on_segment_published(sub.id, time.time())
            self.engine.on_segment_started(sub.id, time.time() + self.sim.broadcast_delay_sec)
        await self._advance(sec)
        track_sec = self._track_sec(sub)
        if track_sec:
            track = self._by_ref[sub.music_ref]
            self.played.append(Played(self._at(), "music", track_sec, sub.id, sub.corner_type,
                                      text=f"{track.title} - {track.artist}"))
            self._current_end = time.time() + track_sec
            if not self.engine_done:
                self.engine.on_track_started({"track_ref": sub.music_ref,
                                              "started_at": time.time() + self.sim.broadcast_delay_sec})
            await self._advance(track_sec)

    async def _close_vote(self, vote: dict) -> None:
        """창이 닫히면 표를 세어 vote.result를 보낸다. 표는 청취자마다 한쪽을 고른 것으로 흉내 낸다."""
        await self._wait_any(self.finished, timeout=self.sim.broadcast_delay_sec + vote["window_sec"])
        first, second = vote["options"]
        lean = self.rng.uniform(0.25, 0.75)  # 투표마다 쏠림이 다르다
        ayes = sum(self.rng.random() < lean for _ in range(self.sim.vote_people))
        vote["counts"] = {first: ayes, second: self.sim.vote_people - ayes}
        vote["closed_at"] = round(self.now(), 1)
        if self.sim.vote_result_delay_sec:
            await self._wait_any(self.finished, timeout=self.sim.vote_result_delay_sec)
        if self.engine_done:
            return
        self.played.append(Played(self._at(), "input", 0, text=(
            f"투표 닫힘 {vote['vote_id']}: {first} {ayes} / {second} {self.sim.vote_people - ayes}")))
        self.engine.on_vote_result({"vote_id": vote["vote_id"], "counts": vote["counts"],
                                    "total": self.sim.vote_people})

    def _at(self) -> float:
        """기록할 송출 시각. 엔진이 끝난 뒤에는 기다리지 않으므로 앞 송출이 끝나는 시각을 쓴다."""
        return max(self.now(), self._cursor)

    async def _advance(self, sec: float) -> None:
        """sec만큼 재생한다. 엔진이 끝나면 남은 대기열은 기다리지 않고 기록만 한다 —
        더 이상 엔진에 알릴 것이 없고, 마지막 곡을 실제로 3~4분 기다릴 이유가 없다."""
        self._cursor = self._at() + sec
        if not self.engine_done:
            await self._wait_any(self.finished, timeout=sec)

    async def report_backpressure(self, engine_finished: asyncio.Event) -> None:
        await self.started.wait()
        while not engine_finished.is_set():
            total = self.remaining_sec()
            severity = "critical" if total <= 0 else "low" if total < self.sim.buffer_low_sec else "normal"
            now = time.time()
            if (self._last_pressure is None or self._last_pressure[0] != severity
                    or now - self._last_pressure[1] >= _BACKPRESSURE_REPEAT_SEC):
                self.engine.report_backpressure({"d_total_ms": int(total * 1000), "severity": severity}, at=now)
                self._last_pressure = (severity, now)
            await self._wait_any(engine_finished, timeout=_BACKPRESSURE_POLL_SEC)

    @staticmethod
    async def _wait_any(*events: asyncio.Event, timeout: float | None = None) -> None:
        waiters = [asyncio.create_task(e.wait()) for e in events]
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
