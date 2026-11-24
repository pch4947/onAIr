"""뒤가 잘린 음원 (#339): 받은 파일의 머리에 적힌 길이가 아니라 변환한 MP3를 실제로 잰 길이로 판단하고 알린다.

잘린 영상은 같은 곡의 다른 영상으로 바꿔 받고, 맞는 것이 없으면 거절한다."""
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import music
from app.config import Settings
from app.music import (MusicService, TrackCandidate, audio_seconds, ffprobe_path, run_process, substitute_order,
                       substitute_query)

VIDEO = 'BaW_jenozKc'
OTHER, THIRD, FOURTH = 'AAAAAAAAAAA', 'BBBBBBBBBBB', 'CCCCCCCCCCC'


def candidate(video_id, title, seconds, channel=''):
    return TrackCandidate(video_id=video_id, url='https://www.youtube.com/watch?v=' + video_id, title=title,
                          channel=channel, duration_ms=round(seconds * 1000), thumbnail_url='')


class SubstituteChoiceTests(unittest.TestCase):
    TITLE = 'Queen – Bohemian Rhapsody (Official Video Remastered)'

    def order(self, *candidates, title=TITLE, seconds=360):
        return [row.video_id for row in substitute_order(list(candidates), VIDEO, title, seconds * 1000)]

    def test_query_drops_video_words_and_keeps_the_song(self):
        self.assertEqual(substitute_query(self.TITLE), 'queen bohemian rhapsody audio')
        self.assertEqual(substitute_query("Girls' Generation 소녀시대 'Gee' MV"), 'girls generation 소녀시대 gee audio')
        # 라이브 영상은 라이브 음원을 찾는다
        self.assertEqual(substitute_query('AC/DC - Thunderstruck (Live At River Plate)'),
                         'ac dc thunderstruck live at river plate audio')

    def test_same_song_of_similar_length_is_chosen(self):
        self.assertEqual(self.order(candidate(OTHER, 'Bohemian Rhapsody (Remastered 2011)', 355, 'Queen - Topic')),
                         [OTHER])

    def test_the_cut_video_itself_is_never_chosen(self):
        self.assertEqual(self.order(candidate(VIDEO, self.TITLE, 360, 'Queen Official')), [])

    def test_length_must_be_close(self):
        self.assertEqual(self.order(candidate(OTHER, 'Queen - Bohemian Rhapsody (Audio)', 344)), [])
        self.assertEqual(self.order(candidate(OTHER, 'Queen - Bohemian Rhapsody (Audio)', 346)), [OTHER])
        self.assertEqual(self.order(candidate(OTHER, 'Queen - Bohemian Rhapsody (Audio)', 376)), [])

    def test_another_song_is_not_chosen(self):
        self.assertEqual(self.order(candidate(OTHER, 'Queen - Somebody To Love (Audio)', 358)), [])

    def test_another_recording_is_not_chosen(self):
        self.assertEqual(self.order(
            candidate(OTHER, 'Queen - Bohemian Rhapsody (Live Aid 1985)', 358),
            candidate(THIRD, 'Bohemian Rhapsody - Queen cover', 360)), [])
        # 반대로 라이브 영상을 스튜디오 음원으로 바꾸지도 않는다
        live = 'Queen - Bohemian Rhapsody (Live at Wembley)'
        self.assertEqual(self.order(candidate(OTHER, 'Queen - Bohemian Rhapsody (Audio)', 360), title=live), [])
        self.assertEqual(self.order(candidate(OTHER, 'Queen - Bohemian Rhapsody Live Wembley audio', 360),
                                    title=live), [OTHER])

    def test_word_inside_another_word_is_not_a_version_word(self):
        self.assertEqual(self.order(candidate(OTHER, 'Olivia - Alive (Audio)', 200), title='Olivia - Alive MV',
                                    seconds=200), [OTHER])

    def test_still_picture_uploads_come_first_then_the_closest_length(self):
        self.assertEqual(self.order(
            candidate(FOURTH, 'Queen - Bohemian Rhapsody', 360),
            candidate(OTHER, 'Queen - Bohemian Rhapsody (Lyrics)', 352),
            candidate(THIRD, 'Bohemian Rhapsody', 358, 'Queen - Topic')), [THIRD, OTHER, FOURTH])

    def test_altered_sound_is_another_recording(self):
        self.assertEqual(self.order(
            candidate(OTHER, 'Queen - Bohemian Rhapsody 12D AUDIO (Multi-directional)', 361),
            candidate(THIRD, 'Queen - Bohemian Rhapsody (432Hz)', 360),
            candidate(FOURTH, 'Queen - Bohemian Rhapsody (Bass Boosted)', 360)), [])

    def test_upload_by_the_same_channel_comes_first(self):
        ids = [row.video_id for row in substitute_order([
            candidate(OTHER, 'Queen - Somebody To Love (Audio)', 310, 'someone'),
            candidate(THIRD, 'Queen - Somebody To Love (Official Lyric Video)', 308, 'Queen Official')],
            VIDEO, 'Queen - Somebody To Love (Official Video)', 310000, 'Queen Official')]
        self.assertEqual(ids, [THIRD, OTHER])

    def test_title_with_only_video_words_finds_nothing(self):
        self.assertEqual(self.order(candidate(OTHER, 'Official Audio', 360), title='Official MV'), [])


@unittest.skipUnless(os.environ.get('ONAIR_FFMPEG'), 'Requires FFmpeg')
class TruncatedAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings('127.0.0.1', 3000, audio_dir=Path(temp.name), ffmpeg=os.environ['ONAIR_FFMPEG'],
                                 lavalink_password='test')
        self.service = MusicService(SimpleNamespace(), self.settings)
        self.directory = self.settings.audio_dir / self.settings.station_id / 'youtube'

    def downloading(self, actual_seconds, declared_seconds, others=None, found=()):
        """실제로는 actual_seconds만 들어 있는데 머리에는 declared_seconds로 적힌 파일을 받은 것처럼 한다.

        others는 다른 영상이 받아지는 모습 {영상: (실제, 적힌 길이) 또는 None(받기 실패)}, found는 검색 결과다.
        """
        self.downloaded = []
        self.searched = None

        async def worker(args, timeout, **kwargs):
            if args[0] != sys.executable:
                return await run_process(args, timeout)
            video, root = args[3], Path(args[4])
            self.downloaded.append(video)
            actual, declared = actual_seconds, declared_seconds
            if video != VIDEO:
                if others[video] is None:
                    raise HTTPException(502, 'Audio preparation failed')
                actual, declared = others[video]
            await run_process([self.settings.ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                               'sine=frequency=440:sample_rate=48000', '-t', str(actual),
                               str(root / 'source.wav')], 15)
            (root / 'result.json').write_text(json.dumps(dict(
                source='source.wav', track_ref='yt:' + video, title='가수 - 노래 MV' if video == VIDEO else '다른 제목',
                artist='가수', duration_ms=round(declared * 1000))), encoding='utf-8')

        async def search(settings, query, limit=3):
            self.searched = (query, limit)
            if isinstance(found, Exception):
                raise found
            return list(found)

        stack = ExitStack()
        stack.enter_context(patch('app.music.run_process', side_effect=worker))
        stack.enter_context(patch('app.music.search_youtube', side_effect=search))
        return stack

    async def test_cut_short_download_is_rejected_and_nothing_is_cached(self):
        with self.downloading(2, 30), self.assertLogs(music.logger, 'WARNING') as logs, \
                self.assertRaises(HTTPException) as raised:
            await self.service.prepare(VIDEO, 'music_cut')
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail['code'], 'audio_truncated')
        self.assertIn('got 2.0s of 30.0s', logs.output[0])
        self.assertIn(VIDEO, logs.output[0])
        # 캐시에 남으면 같은 요청을 다시 보냈을 때 잘린 곡이 나간다
        self.assertFalse(self.service.is_cached('music_cut'))
        self.assertEqual(list(self.directory.glob('music_cut*')), [])

    async def test_cut_short_download_is_replaced_by_another_upload_of_the_song(self):
        found = [candidate(OTHER, '가수 - 노래 (Audio)', 30)]
        with self.downloading(2, 30, {OTHER: (3, 3)}, found), self.assertLogs(music.logger, 'WARNING') as logs:
            audio, metadata = await self.service.prepare(VIDEO, 'music_swapped')
        self.assertEqual(self.searched, ('가수 노래 audio', music.SUBSTITUTE_SEARCH))
        self.assertEqual(self.downloaded, [VIDEO, OTHER])
        # 곡 정보는 신청한 영상의 것이고(제목과 가수는 다듬는다) 길이만 실제로 나갈 소리의 것이다
        self.assertEqual((metadata['track_ref'], metadata['title'], metadata['artist']),
                         ('yt:' + VIDEO, '노래', '가수'))
        self.assertAlmostEqual(metadata['duration_ms'], 3000, delta=150)
        self.assertAlmostEqual(await audio_seconds(ffprobe_path(self.settings), audio), 3.0, delta=0.15)
        self.assertEqual(json.loads((self.directory / 'music_swapped.json').read_text(encoding='utf-8')), metadata)
        self.assertIn(f'Playing {OTHER}', logs.output[-1])
        # 같은 요청을 다시 보내면 캐시에서 같은 곡이 나온다 — 다시 받지 않는다
        with self.downloading(2, 30):
            again, cached = await self.service.prepare(VIDEO, 'music_swapped')
        self.assertEqual((again, cached, self.downloaded), (audio, metadata, []))

    async def test_substitute_that_fails_or_is_cut_too_is_skipped(self):
        found = [candidate(OTHER, '가수 - 노래 (Audio)', 30), candidate(THIRD, '가수 - 노래 (Lyrics)', 31),
                 candidate(FOURTH, '가수 - 노래 가사', 32)]
        with self.downloading(2, 30, {OTHER: None, THIRD: (4, 4)}, found), self.assertLogs(music.logger, 'WARNING'):
            _, metadata = await self.service.prepare(VIDEO, 'music_second')
        self.assertEqual(self.downloaded, [VIDEO, OTHER, THIRD])
        self.assertAlmostEqual(metadata['duration_ms'], 4000, delta=150)
        # 받아 보는 것은 두 개까지다 — 세 번째 후보는 받지 않고 거절한다
        with self.downloading(2, 30, {OTHER: None, THIRD: (2, 31)}, found), \
                self.assertLogs(music.logger, 'WARNING') as logs, self.assertRaises(HTTPException) as raised:
            await self.service.prepare(VIDEO, 'music_none')
        self.assertEqual(raised.exception.detail['code'], 'audio_truncated')
        self.assertEqual(self.downloaded, [VIDEO, OTHER, THIRD])
        self.assertIn(f'Substitute {THIRD}', logs.output[-1])
        self.assertFalse(self.service.is_cached('music_none'))

    async def test_no_usable_substitute_keeps_the_rejection(self):
        for found in ([], [candidate(OTHER, '전혀 다른 곡', 30)], HTTPException(503, 'Search service unavailable')):
            with self.subTest(found=found), self.downloading(2, 30, {}, found), \
                    self.assertLogs(music.logger, 'WARNING'), self.assertRaises(HTTPException) as raised:
                await self.service.prepare(VIDEO, 'music_alone')
            self.assertEqual((raised.exception.status_code, raised.exception.detail['code']), (502, 'audio_truncated'))
            self.assertEqual(self.downloaded, [VIDEO])

    async def test_whole_download_reports_the_length_that_will_actually_play(self):
        # 머리에 적힌 길이와 조금 달라도(허용 5초) 받아들이고, 길이는 MP3를 잰 값으로 알린다
        with self.downloading(2, 4):
            audio, metadata = await self.service.prepare(VIDEO, 'music_whole')
        self.assertIsNone(self.searched)  # 온전한 곡은 다른 영상을 찾지 않는다
        self.assertAlmostEqual(metadata['duration_ms'], 2000, delta=150)
        self.assertAlmostEqual(await audio_seconds(ffprobe_path(self.settings), audio), 2.0, delta=0.15)
        self.assertEqual(json.loads((self.directory / 'music_whole.json').read_text(encoding='utf-8')), metadata)
        self.assertNotIn('source', metadata)

    async def test_tolerance_edge(self):
        with self.downloading(2, 6.5):  # 4.5초 모자람 — 받아들인다
            await self.service.prepare(VIDEO, 'music_near')
        with self.downloading(2, 8), self.assertLogs(music.logger, 'WARNING'), self.assertRaises(HTTPException):
            await self.service.prepare(VIDEO, 'music_far')  # 6초 모자람

    async def test_unreadable_audio_is_a_preparation_failure(self):
        broken = self.settings.audio_dir / 'broken.mp3'
        broken.write_bytes(b'not audio')
        with self.assertRaises(HTTPException) as raised:
            await audio_seconds(ffprobe_path(self.settings), broken)
        self.assertEqual(raised.exception.status_code, 502)
        with self.assertRaises(HTTPException) as missing:
            await audio_seconds(str(self.settings.audio_dir / 'no-such-ffprobe'), broken)
        self.assertEqual(missing.exception.status_code, 503)
