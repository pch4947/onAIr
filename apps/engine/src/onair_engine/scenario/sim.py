"""TTS와 백엔드 송출 흉내 — 엔진은 그대로 두고 바깥만 바꿔 끼운다.

가짜 백엔드는 실제 백엔드(apps/backend/app/streaming/hls.py)가 엔진에 돌려주는 신호만 흉내 낸다.
- 세그먼트를 받은 순서대로 실제 길이만큼 "재생"한다. 곡 소개 멘트 뒤에는 그 곡을 곡 길이만큼 튼다
- 재생을 시작하면 PUBLISHED(송출 이력)와 segment.started(방송 지연을 더한 시각)를 보낸다
- 남은 분량(backpressure)을 상태가 바뀔 때나 10초마다 보낸다
- 다음 곡(track.queued)을 미리 알려 주고, 신청곡이 오면 다음 곡으로 올린다
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
        self._next_ref = 0
        self._current_end = 0.0  # 지금 재생 중인 것이 끝나는 시각(time.time 기준)
        self._last_pressure: tuple[str, float] | None = None

    @property
    def engine_done(self) -> bool:
        """엔진이 끝나 계측이 닫혔다 — 그 뒤로는 엔진에 알리지 않는다."""
        return self.finished.is_set()

    # ── Transport ────────────────────────────────────────────────────
    async def publish_segment(self, sub: SegmentSubmission) -> None:
        self._queue.append(sub)
        self._arrived.set()
        if sub.music_ref and sub.music_ref == self._queued_ref:
            self._queued_ref = None
            self.queue_next_track()  # 소개할 곡을 엔진이 가져갔다 — 백엔드가 다음 곡을 미리 알린다

    async def notify_request_state(self, request_id: str, state) -> None:
        self.states.append((self.now(), request_id, str(state)))

    async def notify_station(self, event_type: str, payload: dict) -> None:
        self.stations.append((self.now(), event_type, payload))
        if event_type == "station.started":
            self.started_at = time.time()
            self.started.set()

    # ── 곡 ───────────────────────────────────────────────────────────
    def queue_next_track(self, request: dict | None = None) -> None:
        """다음 곡을 엔진에 알린다. request는 신청곡 (TrackSpec, request_id, nickname)."""
        if request is not None:
            track = request["track"]
            if self._queued_ref is not None:  # 밀려난 플레이리스트 곡은 다음 차례로 돌린다
                self._playlist.appendleft(self._by_ref[self._queued_ref])
        else:
            track = self._playlist.popleft()
            self._playlist.append(track)
        self._next_ref += 1
        ref = f"sim:{self._next_ref}"
        self._by_ref[ref] = track
        self._queued_ref = ref
        payload = {"track": {"track_ref": ref, "title": track.title, "artist": track.artist,
                             "duration_ms": int(track.sec * 1000)}}
        if request is not None:
            payload.update(source="request", request_id=request["request_id"],
                           nickname=request.get("nickname"))
        self.engine.on_track_queued(payload)

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
