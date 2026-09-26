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
        self.sequence = int(time.time() * 1000)
        self.discontinuities = 0
        self.ready = False
        self.error = None
        self.current = None
        self.task = None
        self.next_publish = 0.0
        self.session = uuid.uuid4().hex

    async def start(self):
        if not shutil.which(self.settings.ffmpeg):
            raise RuntimeError("FFmpeg not found. Set ONAIR_FFMPEG to its executable path.")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.task = asyncio.create_task(self.run(), name="hls-publisher")

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
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
            lines += [f"#EXTINF:{seconds:.6f},", filename]
        temporary = self.directory / "index.m3u8.tmp"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, self.directory / "index.m3u8")
        self.ready = sum(e[1] for e in self.entries) >= 15
        while self.retired and self.retired[0][0] <= time.monotonic():
            _, filename = self.retired.popleft()
            (self.directory / filename).unlink(missing_ok=True)

    async def run(self):
        try:
            while True:
                segment = None
                try:
                    segment_id = await self.redis.lindex(self.prefix + ":queue", 0)
                    if segment_id:
                        raw = await self.redis.hget(self.prefix + ":segments", segment_id)
                        segment = Segment.model_validate_json(raw)
                except RedisError:
                    self.error = "Redis unavailable; playing silence"
                try:
                    source = await asyncio.to_thread(validate_audio, self.settings.audio_dir, segment) if segment else None
                    chunks = await self.encode(source)
                except (ValueError, OSError, TimeoutError):
                    if segment:
                        # Record before removing the bad head so later sources can continue.
                        async with self.redis.pipeline(transaction=True) as pipe:
                            pipe.hset(self.prefix + ":encoding-errors", segment.id, "invalid_or_unreadable_audio")
                            pipe.lpop(self.prefix + ":queue")
                            await pipe.execute()
                    self.error = "Audio encoding failed"
                    await asyncio.sleep(1)
                    continue
                self.current = segment.model_dump() if segment else None
                for index, (name, duration) in enumerate(chunks):
                    await asyncio.sleep(max(0, self.next_publish - time.monotonic()))
                    self.publish(name, duration, index == 0)
                    # Never publish a burst after a slow encode or machine suspension.
                    self.next_publish = time.monotonic() + duration
                if segment:
                    await self.redis.lpop(self.prefix + ":queue")
                self.error = None
        except asyncio.CancelledError:
            raise
        except Exception:
            self.ready = False
            self.error = "HLS publisher stopped; restart backend after checking logs"
            log.exception("HLS publisher stopped")
