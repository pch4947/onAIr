"""HlsPublisher의 VOD 아카이빙: 윈도우 밖으로 밀려난(retired) 세그먼트를 삭제하지 않고
세션별 영구 manifest로 옮기며, stop()은 아직 라이브였던 것까지 마무리하고 flush한다."""
from pathlib import Path
import asyncio
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from redis.exceptions import ConnectionError

from app.config import Settings
from app.streaming.hls import HlsPublisher


def make_publisher(directory, replay_sessions=None):
    settings = Settings('127.0.0.1', 3000, hls_dir=Path(directory))
    publisher = HlsPublisher(None, settings, replay_sessions)
    publisher.directory.mkdir(parents=True, exist_ok=True)
    return publisher


class ArchiveSegmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_moves_file_out_of_live_directory_into_vod(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = make_publisher(directory)
            name = f'{publisher.session}_tok_0.ts'
            (publisher.directory / name).write_bytes(b'data')
            publisher.archive_segment(name, 4.0, True)
            self.assertFalse((publisher.directory / name).is_file())
            self.assertTrue((publisher.directory / 'vod' / publisher.session / name).is_file())

    async def test_manifest_includes_discontinuity_and_extinf(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = make_publisher(directory)
            name = f'{publisher.session}_tok_0.ts'
            (publisher.directory / name).write_bytes(b'data')
            publisher.archive_segment(name, 4.0, True)
            manifest = (publisher.directory / 'vod' / publisher.session / 'index.m3u8').read_text()
            self.assertIn('#EXT-X-DISCONTINUITY', manifest)
            self.assertIn('#EXTINF:4.000000,', manifest)
            self.assertIn(name, manifest)
            self.assertIn('#EXT-X-PLAYLIST-TYPE:EVENT', manifest)
            self.assertNotIn('#EXT-X-ENDLIST', manifest)

    async def test_missing_source_file_is_skipped_without_error(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = make_publisher(directory)
            publisher.archive_segment('missing.ts', 4.0, False)
            self.assertEqual(publisher.vod_entries, [])

    async def test_multiple_archives_accumulate_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = make_publisher(directory)
            for i in range(3):
                name = f'{publisher.session}_tok_{i}.ts'
                (publisher.directory / name).write_bytes(b'data')
                publisher.archive_segment(name, 4.0, i == 0)
            manifest = (publisher.directory / 'vod' / publisher.session / 'index.m3u8').read_text()
            segment_lines = [line for line in manifest.splitlines() if line.endswith('.ts')]
            self.assertEqual(segment_lines, [f'{publisher.session}_tok_{i}.ts' for i in range(3)])

    async def test_disk_failure_during_archive_is_swallowed_not_raised(self):
        # VOD archiving 중 I/O 실패가 publish()의 호출자(run()의 메인 루프)까지 전파되면 안 됨 —
        # 그러면 archive 쓰기 하나 실패한 걸로 라이브 퍼블리셔 전체가 죽어버린다.
        with tempfile.TemporaryDirectory() as directory:
            publisher = make_publisher(directory)
            name = f'{publisher.session}_tok_0.ts'
            (publisher.directory / name).write_bytes(b'data')
            # vod 디렉터리 자리에 일반 파일을 둬서 mkdir(parents=True)가 FileExistsError(OSError의
            # 서브클래스)를 archive_segment 내부에서 던지게 함 — 실제 디스크 풀 상황 없이 재현.
            blocker = publisher.directory / 'vod'
            blocker.write_bytes(b'not a directory')
            try:
                publisher.archive_segment(name, 4.0, False)  # 예외가 발생하면 안 됨
            finally:
                blocker.unlink()
            self.assertEqual(publisher.vod_entries, [])
            # move가 일어나지 않았으니 원본 파일은 그대로 남아있어야 함.
            self.assertTrue((publisher.directory / name).is_file())


class StopFinalizesVodTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_archives_remaining_live_entries_and_finalizes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            replay_sessions = AsyncMock()
            publisher = make_publisher(directory, replay_sessions)
            publisher.started = True
            name = f'{publisher.session}_tok_0.ts'
            (publisher.directory / name).write_bytes(b'data')
            publisher.entries.append((name, 4.0, True))  # 아직 "라이브" 상태, retire된 적 없음
            await publisher.stop()
            self.assertTrue((publisher.directory / 'vod' / publisher.session / name).is_file())
            manifest = (publisher.directory / 'vod' / publisher.session / 'index.m3u8').read_text()
            self.assertIn('#EXT-X-ENDLIST', manifest)
            self.assertIn('#EXT-X-PLAYLIST-TYPE:VOD', manifest)
            replay_sessions.end.assert_awaited_once()

    async def test_stop_without_a_prior_start_does_not_touch_replay_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            replay_sessions = AsyncMock()
            publisher = make_publisher(directory, replay_sessions)
            # self.started는 False로 유지됨: start()가 호출된 적 없음 (hls_enabled=False와 동일한 상황).
            await publisher.stop()
            replay_sessions.end.assert_not_called()

    async def test_stop_with_no_segments_at_all_skips_manifest_write(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = make_publisher(directory)
            publisher.started = True
            await publisher.stop()
            self.assertFalse((publisher.directory / 'vod' / publisher.session).exists())

    async def test_stop_survives_replay_session_redis_failure_and_still_cleans_up(self):
        # stop()에서 replay_sessions.end()가 Redis 장애로 실패해도, 그 아래의 journal/ownership
        # 정리와 호출자 쪽 정리(redis_connection.py의 finally 블록)가 끊기면 안 됨.
        with tempfile.TemporaryDirectory() as directory:
            replay_sessions = AsyncMock()
            replay_sessions.end.side_effect = ConnectionError('offline')
            publisher = make_publisher(directory, replay_sessions)
            publisher.started = True
            fake_journal = Mock()
            fake_ownership = Mock()
            publisher.journal = fake_journal
            publisher.ownership = fake_ownership
            await publisher.stop()  # 예외가 발생하면 안 됨
            fake_journal.close.assert_called_once()
            fake_ownership.close.assert_called_once()

    async def test_stop_survives_manifest_finalize_failure(self):
        # archive_segment가 아닌 stop()이 직접 호출하는 write_vod_manifest(final=True) 쪽의
        # 별도 가드를 검증 — vod/<session> 디렉터리가 없어 파일 쓰기가 실패하는 상황을 재현.
        with tempfile.TemporaryDirectory() as directory:
            publisher = make_publisher(directory)
            publisher.started = True
            publisher.vod_entries.append(('missing.ts', 4.0, False))
            await publisher.stop()  # 예외가 발생하면 안 됨


class StartResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_survives_replay_session_redis_failure(self):
        # start() 중 replay_sessions.start()가 Redis 장애로 실패해도 앱 기동(lifespan) 자체가
        # 죽으면 안 됨 — 라이브 송출은 다시듣기 세션 기록 없이도 정상 동작해야 한다.
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings('127.0.0.1', 3000, hls_dir=Path(directory),
                                ffmpeg=sys.executable, fallback_mode='silence')
            replay_sessions = AsyncMock()
            replay_sessions.start.side_effect = ConnectionError('offline')
            publisher = HlsPublisher(None, settings, replay_sessions)
            try:
                await publisher.start()  # 예외가 발생하면 안 됨
                self.assertTrue(publisher.started)
            finally:
                await publisher.stop()


class VodRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_preserves_archived_retired_then_live_order(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = make_publisher(directory)
            publisher.started = True
            names = ['archived.ts', 'retired1.ts', 'retired2.ts', 'live.ts']
            for name in names:
                (publisher.directory / name).write_bytes(b'audio')
            publisher.archive_segment(names[0], 4, True)
            publisher.retired.extend([(9999, (names[1], 3, False)),
                                      (9999, (names[2], 2, True))])
            publisher.entries.append((names[3], 1, False))
            await publisher.stop()
            manifest = (publisher.directory / 'vod' / publisher.session / 'index.m3u8').read_text()
            self.assertEqual([line for line in manifest.splitlines() if line.endswith('.ts')], names)
            self.assertEqual(manifest.count('#EXT-X-DISCONTINUITY'), 2)
            self.assertTrue(manifest.endswith('#EXT-X-ENDLIST\n'))
            for name in names:
                self.assertTrue((publisher.directory / 'vod' / publisher.session / name).is_file())

    async def test_maintenance_retries_original_session_after_redis_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AsyncMock()
            store.start.side_effect = [ConnectionError('offline'), None]
            publisher = make_publisher(directory, store)
            publisher.replay_started_at = 1234.5
            publisher.journal = Mock(flush=AsyncMock())
            pipe = AsyncMock()
            pipe.__aenter__.return_value = pipe
            pipe.lrange = Mock()
            pipe.hgetall = Mock()
            pipe.execute.return_value = ([], {})
            publisher.redis = Mock(pipeline=Mock(return_value=pipe))
            await publisher.register_replay()
            self.assertFalse(publisher.replay_registered)
            await publisher.register_replay()
            self.assertEqual(store.start.await_count, 1)  # retry interval
            publisher.replay_retry_at = 0
            with patch('app.streaming.hls.asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await publisher.maintain()
            self.assertTrue(publisher.replay_registered)
            self.assertEqual(store.start.await_count, 2)
            for call in store.start.await_args_list:
                self.assertEqual(call.args, (publisher.session, 1234.5))
            await publisher.register_replay()
            self.assertEqual(store.start.await_count, 2)

    async def test_registration_timeout_remains_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AsyncMock()
            store.start.side_effect = TimeoutError
            publisher = make_publisher(directory, store)
            publisher.replay_started_at = 1000
            await publisher.register_replay()
            self.assertFalse(publisher.replay_registered)
            store.start.side_effect = None
            publisher.replay_retry_at = 0
            await publisher.register_replay()
            self.assertTrue(publisher.replay_registered)
