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
    def __init__(self, redis, settings, replay_sessions=None):
        self.redis, self.settings = redis, settings
        self.replay_sessions = replay_sessions
        self.prefix = f"backend:{settings.station_id}"
        self.directory = settings.hls_dir / settings.station_id
        self.entries = deque()
        self.retired = deque()
        self.vod_entries = []
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
        self.started = False
        self.replay_started_at = None
        self.replay_registered = False
        self.replay_retry_at = 0.0
        self.journal = None
        self.ownership = None
        self.maintenance = None
        self.preparation = None
        self.fallback_source = None
        self.fallback_cache = None
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
        self.journal = Journal(self.directory / 'broadcast.sqlite', self.settings.station_id,
                               repeat_playlist=self.settings.track_intro_enabled)
        self.recovery.extend(self.journal.recover())
        if self.settings.fallback_mode == 'music':
            if self.settings.fallback_audio:
                self.fallback_source = self.settings.fallback_audio
            elif self.settings.bgm_audio:
                self.fallback_source = self.settings.bgm_audio
            else:
                self.fallback_source = self.directory / 'fallback.wav'
                await asyncio.to_thread(write_fallback, self.fallback_source)
        self.started = True
        self.replay_started_at = time.time()
        self.task = asyncio.create_task(self.run(), name="hls-publisher")
        await self.register_replay()
        self.maintenance = asyncio.create_task(self.maintain(), name='broadcast-maintenance')

    async def register_replay(self):
        if (not self.replay_sessions or self.replay_registered
                or self.replay_started_at is None or time.monotonic() < self.replay_retry_at):
            return
        self.replay_retry_at = time.monotonic() + 5
        try:
            async with asyncio.timeout(3):
                await self.replay_sessions.start(self.session, self.replay_started_at)
            self.replay_registered = True
        except (RedisError, TimeoutError):
            log.warning('Replay registration deferred for %s; retrying', self.session)

    async def stop(self):
        for task in (self.task, self.maintenance, self.preparation):
            if task:
                task.cancel()
        for task in (self.task, self.maintenance, self.preparation):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.started:
            # 아직 라이브/유예기간 중이던 것까지 archive해서 stop 시점에 방송 끝부분이 날아가지 않게 함.
            # 여기서부터는 어떤 것도 예외를 던지면 안 됨: 아래의 세션/저널/소유권 정리와
            # 호출자 쪽 정리(Redis 클라이언트 종료 등)가 반드시 끝까지 실행돼야 하기 때문.
            # Retired chunks precede the current live window in broadcast order.
            for _, (name, duration, discontinuity) in self.retired:
                self.archive_segment(name, duration, discontinuity)
            self.retired.clear()
            for name, duration, discontinuity in self.entries:
                self.archive_segment(name, duration, discontinuity)
            self.entries.clear()
            if self.vod_entries:
                try:
                    self.write_vod_manifest(final=True)
                except OSError:
                    log.exception('Failed to finalize VOD manifest for session %s', self.session)
            if self.replay_sessions:
                try:
                    self.replay_retry_at = 0.0
                    await self.register_replay()
                    async with asyncio.timeout(3):
                        await self.replay_sessions.end(self.session, time.time())
                except (RedisError, TimeoutError):
                    log.warning('Failed to record replay session end for %s; Redis unavailable', self.session)
        if self.journal:
            self.journal.close()
            self.journal = None
        if self.ownership:
            self.ownership.close()
            self.ownership = None
        self.ready = False
        self.started = False

    async def encode(self, source: Path | None, *, bgm_duration=None):
        # Only completed encodes are exposed; staging is outside the HTTP directory.
        with tempfile.TemporaryDirectory(prefix="onair-hls-") as staging:
            output = Path(staging)
            args = [self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
            if source:
                args += ["-i", str(source)]
            else:
                args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "4"]
            if bgm_duration is not None:
                args += ['-stream_loop', '-1', '-i', str(self.settings.bgm_audio),
                    '-filter_complex',
                    '[0:a]adenorm=level=-90,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[voice];'
                    '[1:a]aresample=48000,volume=0.177828,afade=t=in:d=0.3,'
                    f'afade=t=out:st={max(0, bgm_duration - 0.3):.6f}:d=0.3[bed];'
                    '[voice][bed]amix=inputs=2:duration=first:normalize=0,'
                    'alimiter=limit=0.95:level=0[out]', '-map', '[out]', '-t', str(bgm_duration)]
            else:
                # loudnorm can emit NaN for short, completely silent TTS output.
                args += ['-map', '0:a:0', '-af', 'adenorm=level=-90,loudnorm=I=-16:TP=-1.5:LRA=11']
            args += ["-vn",
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
            self.retired.append((time.monotonic() + 60, old))
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
            _, (filename, duration, discontinuity) = self.retired.popleft()
            self.archive_segment(filename, duration, discontinuity)
            self.fragment_metadata.pop(filename, None)
            self.fallback_fragments.discard(filename)
            self.fragment_times.pop(filename, None)

    def archive_segment(self, name, duration, discontinuity):
        # 라이브 윈도우 밖으로 복사가 아닌 이동(move)을 해서 영구적으로 커지기만 하는 VOD 아카이브로 옮김.
        # 최선 노력(best-effort): archive가 실패해도 라이브 퍼블리셔는 절대 멈추면 안 됨(write_vod_manifest 참고).
        source = self.directory / name
        if not source.is_file():
            return
        try:
            vod_dir = self.directory / "vod" / self.session
            vod_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(vod_dir / name))
            self.vod_entries.append((name, duration, discontinuity))
            self.write_vod_manifest(final=False)
        except OSError:
            log.exception('VOD archiving failed for %s; live playback continues', name)

    def write_vod_manifest(self, final):
        vod_dir = self.directory / "vod" / self.session
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:5",
                 f"#EXT-X-PLAYLIST-TYPE:{'VOD' if final else 'EVENT'}"]
        for filename, seconds, discontinuity in self.vod_entries:
            if discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            lines += [f"#EXTINF:{seconds:.6f},", filename]
        if final:
            lines.append("#EXT-X-ENDLIST")
        temporary = vod_dir / "index.m3u8.tmp"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, vod_dir / "index.m3u8")

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
        if self.current:
            covered.add(self.current['id'])
        queued = sum(seconds for key, seconds in self.queued.items() if key not in covered) if self.queue_available else None
        return {'bufferSeconds': round(encoded, 3), 'queuedAudioSeconds': queued,
                'totalAudioEstimateSeconds': round(encoded + queued, 3) if queued is not None else None,
                'bufferLow': encoded < self.settings.buffer_low_seconds,
                'fallbackActive': self.fallback_active, 'queueAvailable': self.queue_available,
                'broadcastDelaySeconds': self.settings.broadcast_delay_seconds,
                'pendingStateEvents': self.journal.pending() if self.journal else 0}

    async def remove_head(self, segment_id):
        await self.redis.eval("return redis.call('LREM', KEYS[1], 0, ARGV[1])",
                              1, self.prefix + ':queue', segment_id)

    async def select_next(self):
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.lrange(self.prefix + ':queue', 0, -1)
            pipe.hgetall(self.prefix + ':segments')
            ids, records = await pipe.execute()
        if self.settings.track_intro_enabled:
            # A submitted introduction reserves its exact audio, even when a
            # newer request/vote replaced the next-track announcement.
            target = None
            if self.current and self.current.get('kind') in ('speech', 'glue') and self.current.get('music_ref'):
                target = await self.redis.hget(self.prefix + ':intro-tracks', self.current['id'])
            if target:
                if target in records and self.journal.status(target) not in ('PUBLISHED', 'PLAYED', 'FAILED'):
                    return Segment.model_validate_json(records[target])
                # A failed reservation must not terminate the entire station.
                # Other speech can continue while the engine chooses another track.
                return None
            ids = [key for key in ids if key in records and json.loads(records[key])['kind'] != 'music']
        candidates = []
        for key in ids:
            if self.current and key == self.current['id']:
                continue
            if self.journal.status(key) in ('PUBLISHED', 'PLAYED', 'FAILED'):
                await self.remove_head(key)
                continue
            row = Segment.model_validate_json(records[key])
            candidates.append(row)
            # A fixed segment is a barrier: it may pass preceding movable items,
            # but later arrivals cannot pass it.
            if not row.reorderable:
                break
        if not candidates:
            return None
        chosen = candidates[0]
        for row in candidates[1:]:
            if (row.kind == 'ack' or (row.kind == 'speech' and row.corner_type == 'request_reply')) and row.priority < chosen.priority:
                chosen = row
        return chosen

    async def maintain(self):
        while True:
            await self.register_replay()
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
                        if self.settings.track_intro_enabled:
                            pipe.hget(self.prefix + ':track-plan', 'frozen')
                        queue_snapshot = await pipe.execute()
                        ids, records = queue_snapshot[:2]
                    reserved = queue_snapshot[2] if self.settings.track_intro_enabled else None
                    self.queued = {}
                    for key in ids:
                        if key not in records:
                            continue
                        row = json.loads(records[key])
                        # Unintroduced music is a candidate, not playable buffer.
                        # Counting a four-minute candidate here can prevent the
                        # engine from generating the introduction needed to play it.
                        if self.settings.track_intro_enabled and row['kind'] == 'music' and key != reserved:
                            continue
                        self.queued[key] = row['duration_ms'] / 1000
                    self.queue_available = True
                    self.redis_error = None
                    # Retry an interrupted queue removal without replaying a published source.
                    for segment_id in ids:
                        if self.journal.status(segment_id) in ('PUBLISHED', 'PLAYED', 'FAILED'):
                            await self.remove_head(segment_id)
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
                    segment = await self.select_next()
                    if segment is None:
                        return None
            if segment.request_ref:
                async with asyncio.timeout(3):
                    raw_state = await self.redis.hget(self.prefix + ':request-states', segment.request_ref)
                if raw_state and json.loads(raw_state)['state'] in ('rejected', 'failed'):
                    self.journal.transition(segment.model_dump(), 'FAILED')
                    if recovered:
                        self.recovery.popleft()
                    return None
            self.journal.transition(segment.model_dump(), 'MIXING')
            started = time.monotonic()
            source = await asyncio.to_thread(validate_audio, self.settings.audio_dir, segment)
            try:
                if self.settings.bgm_audio and segment.kind in ('speech', 'ack', 'filler', 'glue'):
                    chunks = await self.encode(source, bgm_duration=segment.duration_ms / 1000)
                else:
                    chunks = await self.encode(source)
            except TimeoutError as exc:
                raise ValueError('Audio encoding timed out') from exc
            self.journal.measure(segment.id, 'encode', (time.monotonic() - started) * 1000)
            self.unpublished_seconds += sum(duration for _, duration in chunks)
            self.active_id = segment.id
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
        if self.fallback_cache is None:
            try:
                chunks = await self.encode(self.fallback_source)
            except (ValueError, OSError, TimeoutError):
                self.error = 'Fallback music unavailable; using silence'
                self.fallback_source = None
                chunks = await self.encode(None)
            self.fallback_cache = []
            for name, duration in chunks:
                path = self.directory / name
                self.fallback_cache.append((path.read_bytes(), duration))
                path.unlink()
        chunks = []
        token = uuid.uuid4().hex
        for index, (data, duration) in enumerate(self.fallback_cache):
            name = f'{self.session}_{token}_{index}.ts'
            (self.directory / name).write_bytes(data)
            chunks.append((name, duration))
        return chunks

    async def wait_for_next(self):
        # Keep polling while current audio plays, with at most one prepared source.
        while True:
            prepared = await self.prepare_next()
            if prepared:
                return prepared
            await asyncio.sleep(0.1)

    async def run(self):
        try:
            while True:
                if self.preparation is None:
                    self.preparation = asyncio.create_task(self.wait_for_next())
                # Source encoding is independent of paced fallback publishing.
                await asyncio.sleep(max(0, self.next_publish - time.monotonic()))
                done, _ = await asyncio.wait({self.preparation}, timeout=0.1 if not self.entries else 0)
                prepared = None
                if done:
                    prepared = self.preparation.result()
                    self.preparation = None
                if prepared:
                    segment, chunks = prepared
                    self.current = segment
                    self.fallback_active = False
                    self.error = None
                    self.active_id = None
                    self.preparation = asyncio.create_task(self.wait_for_next())
                else:
                    segment = None
                    self.current = None
                    self.fallback_active = True
                    chunks = await self.fallback_chunks()
                for index, (name, duration) in enumerate(chunks):
                    await asyncio.sleep(max(0, self.next_publish - time.monotonic()))
                    self.publish(name, duration, index == 0)
                    if index == 0 and segment and segment['kind'] == 'music' and segment.get('music_ref'):
                        self.journal.track_started(segment, time.time() + self.settings.broadcast_delay_seconds)
                    start = time.monotonic() + self.settings.broadcast_delay_seconds
                    self.timeline.append((start, start + duration, segment is not None))
                    self.next_publish = time.monotonic() + duration
                    if segment:
                        self.unpublished_seconds = max(0, self.unpublished_seconds - duration)
                if segment:
                    self.journal.transition(segment, 'PUBLISHED')
                    self.finishing.append((self.next_publish + self.settings.broadcast_delay_seconds, segment))
                    # State and removal retry in maintenance; publishing never waits for Redis.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.ready = False
            self.error = 'HLS publisher stopped; check local storage and FFmpeg'
            log.exception('HLS publisher stopped')
