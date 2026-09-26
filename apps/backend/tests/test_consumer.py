import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from redis.asyncio import Redis

from app.config import Settings
from app.messaging.contracts import Segment, validate_audio
from app.messaging.consumer import EngineConsumer, GROUP


def payload(station="st_test"):
    return dict(id="seg_one", station_id=station, audio_ref=f"{station}/one.wav",
                duration_ms=1000, corner_type="opening", kind="speech", priority=50,
                reorderable=True, request_ref=None, music_ref=None, created_at=1.0)


class AudioPathTests(unittest.TestCase):
    def test_traversal_and_cross_station(self):
        with tempfile.TemporaryDirectory() as directory:
            for ref in ["../escape.wav", "/tmp/a.wav", "C:/a.wav", "st_test/../a.wav",
                        "other/one.wav", "st_test\\one.wav", "st_test/a.wav:stream", "st_test/.temp.wav"]:
                with self.subTest(ref=ref), self.assertRaises(ValueError):
                    validate_audio(Path(directory), Segment(**{**payload(), "audio_ref": ref}))

    def test_missing_file_and_shared_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seg = Segment(**payload())
            with self.assertRaises(FileNotFoundError):
                validate_audio(root, seg)
            (root / "st_test").mkdir()
            (root / seg.audio_ref).write_bytes(b"test")
            self.assertEqual(validate_audio(root, seg), root / seg.audio_ref)


@unittest.skipUnless(os.environ.get("ONAIR_INTEGRATION_TEST") == "1",
                     "Requires local Redis and engine package; set ONAIR_INTEGRATION_TEST=1")
class ConsumerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
        await self.redis.ping()
        self.directory = tempfile.TemporaryDirectory()
        self.station = "st_test_" + uuid.uuid4().hex
        self.settings = Settings("127.0.0.1", 3000, station_id=self.station,
                                 audio_dir=Path(self.directory.name))
        self.consumer = EngineConsumer(self.redis, self.settings)
        await self.consumer.ensure_group()

    async def asyncTearDown(self):
        # Only this test's UUID station keys; never FLUSHDB or user station data.
        for pattern in [f"backend:{self.station}:*", f"engine:{self.station}:*"]:
            keys = [key async for key in self.redis.scan_iter(match=pattern)]
            if keys:
                await self.redis.delete(*keys)
        await self.redis.aclose()
        self.directory.cleanup()

    async def deliver(self, data, kind="segment.submitted"):
        fields = dict(type=kind, contract_version="1", event_id="evt_"+uuid.uuid4().hex,
                      station_id=self.station, at="1", payload=json.dumps(data))
        entry_id = await self.redis.xadd(self.consumer.stream, fields)
        await self.redis.xreadgroup(GROUP, self.consumer.name, {self.consumer.stream: ">"})
        await self.consumer.handle(entry_id, fields)
        return entry_id, fields

    async def test_real_engine_duplicate_and_restart(self):
        from onair_engine.main import load_config
        from onair_engine.manager import EngineManager
        engine_dir = Path(__file__).resolve().parents[2] / "engine"
        config, settings = load_config(engine_dir / "config/station.example.yaml")
        config.station_id = self.station
        settings = replace(settings, transport_kind="redis", transport_redis_url=self.settings.redis_url,
                           audio_dir=self.settings.audio_dir, tts_cache_dir=None,
                           sqlite_path=self.settings.audio_dir / "metrics.sqlite",
                           safety_rules_path=engine_dir / "config/safety_rules.yaml")
        await asyncio.wait_for(EngineManager(settings).run_local(config, max_segments=1), 30)
        batches = await self.redis.xreadgroup(GROUP, self.consumer.name, {self.consumer.stream: ">"})
        self.assertTrue(batches)
        entries = batches[0][1]
        for entry_id, fields in entries:
            await self.consumer.handle(entry_id, fields)
        count = await self.redis.llen(self.consumer.prefix + ":queue")
        self.assertGreater(count, 0)
        # New delivery of same engine payload with another event ID stays deduplicated.
        await self.deliver(json.loads(entries[0][1]["payload"]))
        self.assertEqual(await self.redis.llen(self.consumer.prefix + ":queue"), count)
        # Save succeeds but ACK is interrupted; a new consumer reclaims pending work.
        original = json.loads(entries[0][1]["payload"])
        original["id"] = "seg_before_crash"
        original_ack = self.consumer.ack
        async def crash(_):
            raise RuntimeError("simulated process exit before ACK")
        self.consumer.ack = crash
        with self.assertRaises(RuntimeError):
            await self.deliver(original)
        self.consumer.ack = original_ack
        replacement = EngineConsumer(self.redis, self.settings)
        reclaimed = await self.redis.xautoclaim(self.consumer.stream, GROUP, replacement.name, 0, "0-0")
        self.assertTrue(reclaimed[1])
        for entry_id, fields in reclaimed[1]:
            await replacement.handle(entry_id, fields)
        self.assertEqual(await self.redis.llen(self.consumer.prefix + ":queue"), count + 1)
        self.assertEqual((await self.redis.xpending(self.consumer.stream, GROUP))["pending"], 0)

    async def test_bad_paths_retry_conflict_and_state_order(self):
        data = payload(self.station)
        await self.deliver({**data, "audio_ref": "../outside.wav"})
        entry_id, fields = await self.deliver(data)
        await self.consumer.handle(entry_id, fields)
        await self.consumer.handle(entry_id, fields)
        self.assertEqual(await self.redis.xlen(self.consumer.stream + ":dead"), 2)
        path = self.settings.audio_dir / data["audio_ref"]
        path.parent.mkdir()
        path.write_bytes(b"test")
        await self.deliver(data)
        await self.deliver({**data, "duration_ms": 2000})
        self.assertEqual(await self.redis.llen(self.consumer.prefix + ":queue"), 1)
        self.assertEqual(await self.redis.xlen(self.consumer.stream + ":dead"), 3)
        old_id, old_fields = await self.deliver({"request_id": "req_one", "state": "generating"}, "request.state")
        await self.deliver({"request_id": "req_one", "state": "generated"}, "request.state")
        await self.consumer.handle(old_id, old_fields)
        state = json.loads(await self.redis.hget(self.consumer.prefix + ":request-states", "req_one"))
        self.assertEqual(state["state"], "generated")

    async def test_lifespan_background_loop(self):
        from app.main import create_app
        from onair_engine.pipeline.tts import DummyTtsClient
        from onair_engine.domain import SegmentSubmission, SegmentKind
        from onair_engine.transport import RedisTransport
        app = create_app(self.settings)
        data = payload(self.station)
        duration = await DummyTtsClient().synthesize("연결 테스트", self.settings.audio_dir / data["audio_ref"])
        data.update(duration_ms=duration, kind=SegmentKind.SPEECH)
        transport = RedisTransport(self.settings.redis_url, self.station)
        try:
            async with app.router.lifespan_context(app):
                await transport.publish_segment(SegmentSubmission(**data))
                async with asyncio.timeout(10):
                    while await self.redis.llen(self.consumer.prefix + ":queue") != 1:
                        await asyncio.sleep(0.1)
                self.assertEqual((await self.redis.xpending(self.consumer.stream, GROUP))["pending"], 0)
        finally:
            await transport.close()
