"""Encode source files separately; publish a sliding playlist at audio speed."""

import asyncio
from collections import deque
import contextlib
import logging
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid
import json
from datetime import datetime, timezone

from .journal import Journal
from .fallback import write_fallback
from .ownership import StationLock

from redis.exceptions import RedisError

from ..messaging.contracts import Segment, validate_audio

log = logging.getLogger(__name__)


class HlsPublisher:
    def __init__(self, redis, settings):
        self.redis, self.settings = redis, settings
        self.prefix = f"backend:{settings.station_id}"
        self.directory = settings.hls_dir / settings.station_id
        self.entries = deque()
        self.retired = deque()
        # Only published chunks are indexed; keep retired entries during the TS grace period.
        self.fragment_metadata = {}
        self.fallback_fragments = set()
        self.fragment_times = {}
        self.sequence = int(time.time() * 1000)
        self.discontinuities = 0
        self.ready = False
        self.error = None
        self.current = None
        self.task = None
        self.next_publish = 0.0
        self.session = uuid.uuid4().hex
        self.journal = None
        self.ownership = None
        self.maintenance = None
        self.preparation = None
        self.fallback_source = None
        self.fallback_active = False
        self.timeline = deque()
        self.finishing = deque()
        self.recovery = deque()
        self.queued = {}
        self.queue_available = False
        self.unpublished_seconds = 0.0
        self.active_id = None
        self.last_pressure = None
        self.last_pressure_at = 0.0
        self.redis_error = None

    async def start(self):
        if not shutil.which(self.settings.ffmpeg):
            raise RuntimeError("FFmpeg not found. Set ONAIR_FFMPEG to its executable path.")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ownership = StationLock(self.directory / 'publisher.lock')
        self.journal = Journal(self.directory / 'broadcast.sqlite', self.settings.station_id)
        self.recovery.extend(self.journal.recover())
        if self.settings.fallback_mode == 'music':
            if self.settings.fallback_audio:
                self.fallback_source = self.settings.fallback_audio
            else:
                self.fallback_source = self.directory / 'fallback.wav'
                await asyncio.to_thread(write_fallback, self.fallback_source)
        self.maintenance = asyncio.create_task(self.maintain(), name='broadcast-maintenance')
        self.task = asyncio.create_task(self.run(), name="hls-publisher")

    async def stop(self):
        for task in (self.task, self.maintenance, self.preparation):
            if task:
                task.cancel()
        for task in (self.task, self.maintenance, self.preparation):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.journal:
            self.journal.close()
            self.journal = None
        if self.ownership:
            self.ownership.close()
            self.ownership = None
        self.ready = False

    async def encode(self, source: Path | None):
        # Only completed encodes are exposed; staging is outside the HTTP directory.
        with tempfile.TemporaryDirectory(prefix="onair-hls-") as staging:
            output = Path(staging)
            args = [self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
            if source:
                args += ["-i", str(source)]
            else:
                args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "4"]
            args += ["-map", "0:a:0", "-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                     "-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", "128k",
                     "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
                     "-hls_segment_filename", str(output / "chunk_%05d.ts"), str(output / "local.m3u8")]
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(120):
                    await process.communicate()
                if process.returncode:
                    raise ValueError("Audio decode/encode failed")
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
            chunks = []
            duration = None
            token = uuid.uuid4().hex
            for line in (output / "local.m3u8").read_text().splitlines():
                if line.startswith("#EXTINF:"):
                    duration = float(line.split(":", 1)[1].rstrip(","))
                elif line and not line.startswith("#"):
                    if duration is None or not math.isfinite(duration) or not 0 < duration <= 5:
                        raise ValueError("Invalid encoded HLS chunk duration")
                    name = f"{self.session}_{token}_{len(chunks)}.ts"
                    temporary = self.directory / (name + ".tmp")
                    shutil.copyfile(output / line, temporary)
                    os.replace(temporary, self.directory / name)
                    chunks.append((name, duration))
                    duration = None
            if not chunks:
                raise ValueError("No audio frames")
            return chunks

    def publish(self, name, duration, boundary):
        self.fragment_times[name] = datetime.fromtimestamp(
            time.time() + self.settings.broadcast_delay_seconds, timezone.utc).isoformat(timespec='milliseconds')
        self.entries.append((name, duration, boundary))
        # Keep at least 24 seconds; duration-based because source tails may be short.
        while len(self.entries) > 1 and sum(e[1] for e in self.entries) - self.entries[0][1] >= 24:
            old = self.entries.popleft()
            self.sequence += 1
            self.discontinuities += int(old[2])
            self.retired.append((time.monotonic() + 60, old[0]))
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:5",
                 f"#EXT-X-MEDIA-SEQUENCE:{self.sequence}",
                 f"#EXT-X-DISCONTINUITY-SEQUENCE:{self.discontinuities}"]
        for filename, seconds, discontinuity in self.entries:
            if discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            lines += ['#EXT-X-PROGRAM-DATE-TIME:' + self.fragment_times[filename],
                      f"#EXTINF:{seconds:.6f},", filename]
        temporary = self.directory / "index.m3u8.tmp"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, self.directory / "index.m3u8")
        self.fragment_metadata[name] = {
            "id": self.current["id"],
            "corner_type": self.current["corner_type"],
            "kind": self.current["kind"],
            "duration_ms": self.current["duration_ms"],
        } if self.current else None
        if self.fallback_active and self.fallback_source:
            self.fallback_fragments.add(name)
        self.ready = sum(e[1] for e in self.entries) >= 15
        while self.retired and self.retired[0][0] <= time.monotonic():
            _, filename = self.retired.popleft()
            (self.directory / filename).unlink(missing_ok=True)
            self.fragment_metadata.pop(filename, None)
            self.fallback_fragments.discard(filename)
            self.fragment_times.pop(filename, None)

    def playback_metadata(self, fragment):
        # Never substitute the latest publisher.current for an unknown/old client fragment.
        if fragment not in self.fragment_metadata:
            return {"fragment": fragment, "status": "unavailable", "segment": None}
        segment = self.fragment_metadata[fragment]
        status = 'segment' if segment else 'fallback' if fragment in self.fallback_fragments else 'silence'
        return {"fragment": fragment, "status": status, "segment": segment,
                'broadcastStartAt': self.fragment_times.get(fragment)}

    def buffer_snapshot(self):
        now = time.monotonic()
        scheduled = sum(max(0, end - max(start, now)) for start, end, real in self.timeline if real)
        encoded = scheduled + self.unpublished_seconds
        covered = {segment['id'] for _, segment in self.finishing}
        if self.active_id:
            covered.add(self.active_id)
        queued = sum(seconds for key, seconds in self.queued.items() if key not in covered) if self.queue_available else None
        return {'bufferSeconds': round(encoded, 3), 'queuedAudioSeconds': queued,
                'totalAudioEstimateSeconds': round(encoded + queued, 3) if queued is not None else None,
                'bufferLow': encoded < self.settings.buffer_low_seconds,
                'fallbackActive': self.fallback_active, 'queueAvailable': self.queue_available,
                'broadcastDelaySeconds': self.settings.broadcast_delay_seconds,
                'pendingStateEvents': self.journal.pending() if self.journal else 0}

    async def remove_head(self, segment_id):
        await self.redis.eval("if redis.call('LINDEX', KEYS[1], 0) == ARGV[1] then return redis.call('LPOP', KEYS[1]) end",
                              1, self.prefix + ':queue', segment_id)

    async def maintain(self):
        while True:
            now = time.monotonic()
            while self.timeline and self.timeline[0][1] <= now:
                self.timeline.popleft()
            while self.finishing and self.finishing[0][0] <= now:
                _, segment = self.finishing.popleft()
                self.journal.transition(segment, 'PLAYED')
            try:
                async with asyncio.timeout(3):
                    await self.journal.flush(self.redis)
                    async with self.redis.pipeline(transaction=True) as pipe:
                        pipe.lrange(self.prefix + ':queue', 0, -1)
                        pipe.hgetall(self.prefix + ':segments')
                        ids, records = await pipe.execute()
                    self.queued = {key: json.loads(records[key])['duration_ms'] / 1000 for key in ids if key in records}
                    self.queue_available = True
                    self.redis_error = None
                    # Retry an interrupted queue removal without replaying a published source.
                    if ids and self.journal.status(ids[0]) in ('PUBLISHED', 'PLAYED', 'FAILED'):
                        await self.remove_head(ids[0])
                    snapshot = self.buffer_snapshot()
                    total = snapshot['totalAudioEstimateSeconds']
                    severity = 'critical' if total <= 0 else 'low' if total < self.settings.buffer_low_seconds else 'normal'
                    if severity != self.last_pressure or now - self.last_pressure_at >= 10:
                        # Keep only the latest pressure measurement during outages.
                        self.journal.enqueue('backpressure', dict(d_total_ms=int(total * 1000),
                            threshold_ms=int(self.settings.buffer_low_seconds * 1000), severity=severity))
                        self.last_pressure, self.last_pressure_at = severity, now
            except (RedisError, TimeoutError):
                self.queue_available = False
                self.redis_error = 'Redis unavailable; local playout continues'
            except Exception:
                log.exception('Broadcast maintenance failed; retrying')
                self.queue_available = False
                self.redis_error = 'Broadcast state synchronization failed'
            await asyncio.sleep(0.5)

    async def prepare_next(self):
        segment = None
        recovered = False
        try:
            if self.recovery:
                segment = Segment.model_validate(self.recovery[0])
                recovered = True
            else:
                async with asyncio.timeout(3):
                    segment_id = await self.redis.lindex(self.prefix + ':queue', 0)
                    if not segment_id:
                        return None
                    if self.journal.status(segment_id) in ('PUBLISHED', 'PLAYED', 'FAILED'):
                        await self.remove_head(segment_id)
                        return None
                    raw = await self.redis.hget(self.prefix + ':segments', segment_id)
                segment = Segment.model_validate_json(raw)
            if segment.request_ref:
                async with asyncio.timeout(3):
                    raw_state = await self.redis.hget(self.prefix + ':request-states', segment.request_ref)
                if raw_state and json.loads(raw_state)['state'] in ('rejected', 'failed'):
                    self.journal.transition(segment.model_dump(), 'FAILED')
                    if recovered:
                        self.recovery.popleft()
                    return None
            self.active_id = segment.id
            self.journal.transition(segment.model_dump(), 'MIXING')
            started = time.monotonic()
            source = await asyncio.to_thread(validate_audio, self.settings.audio_dir, segment)
            try:
                chunks = await self.encode(source)
            except TimeoutError as exc:
                raise ValueError('Audio encoding timed out') from exc
            self.journal.measure(segment.id, 'encode', (time.monotonic() - started) * 1000)
            self.unpublished_seconds = sum(duration for _, duration in chunks)
            if recovered:
                self.recovery.popleft()
            return segment.model_dump(), chunks
        except (RedisError, TimeoutError):
            self.redis_error = 'Redis unavailable; local playout continues'
            self.active_id = None
            return None
        except (ValueError, OSError):
            log.warning('Invalid audio source; skipping', exc_info=True)
            if segment:
                self.journal.transition(segment.model_dump(), 'FAILED')
                if recovered:
                    self.recovery.popleft()
            self.active_id = None
            self.error = 'Audio encoding failed; using fallback'
            return None

    async def fallback_chunks(self):
        try:
            return await self.encode(self.fallback_source)
        except (ValueError, OSError, TimeoutError):
            self.error = 'Fallback music unavailable; using silence'
            self.fallback_source = None
            return await self.encode(None)

    async def run(self):
        try:
            while True:
                if self.preparation is None:
                    self.preparation = asyncio.create_task(self.prepare_next())
                # Source encoding is independent of paced fallback publishing.
                done, _ = await asyncio.wait({self.preparation}, timeout=0.1)
                prepared = None
                if done:
                    prepared = self.preparation.result()
                    self.preparation = None
                if prepared:
                    segment, chunks = prepared
                    self.current = segment
                    self.fallback_active = False
                    self.error = None
                else:
                    segment = None
                    self.current = None
                    self.fallback_active = True
                    chunks = await self.fallback_chunks()
                for index, (name, duration) in enumerate(chunks):
                    await asyncio.sleep(max(0, self.next_publish - time.monotonic()))
                    self.publish(name, duration, index == 0)
                    start = time.monotonic() + self.settings.broadcast_delay_seconds
                    self.timeline.append((start, start + duration, segment is not None))
                    self.next_publish = time.monotonic() + duration
                    if segment:
                        self.unpublished_seconds = max(0, self.unpublished_seconds - duration)
                if segment:
                    self.journal.transition(segment, 'PUBLISHED')
                    self.finishing.append((self.next_publish + self.settings.broadcast_delay_seconds, segment))
                    self.active_id = None
                    self.unpublished_seconds = 0
                    # State and removal retry in maintenance; publishing never waits for Redis.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.ready = False
            self.error = 'HLS publisher stopped; check local storage and FFmpeg'
            log.exception('HLS publisher stopped')
