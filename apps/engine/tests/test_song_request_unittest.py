"""Song announcements carry only L0-checked requester names into the prompt."""
import unittest
from unittest.mock import Mock

from onair_engine.corners.steps import _music_intro
from onair_engine.domain import Material
from onair_engine.engine import StationEngine


class SongRequestTests(unittest.TestCase):
    def test_requester_name_is_filtered_before_track_is_used(self):
        engine = StationEngine.__new__(StationEngine)
        engine.tracks = Mock()
        engine._safety = Mock()
        engine._safety.check_l0.side_effect = lambda text: 'unsafe' if 'https://' in text else None
        track = {'track_ref': 'yt:test', 'title': '곡', 'artist': '가수'}
        engine.on_track_queued({'track': track, 'source': 'request', 'nickname': '청취자'})
        safe = engine.tracks.queue.call_args.args[0]
        self.assertEqual(safe['requester_nickname'], '청취자')
        self.assertIn('청취자', _music_intro(Material(track=safe)))
        engine.on_track_queued({'track': track, 'source': 'request', 'nickname': 'https://example.com'})
        self.assertNotIn('requester_nickname', engine.tracks.queue.call_args.args[0])
        engine.on_track_queued({'track': track, 'source': 'playlist', 'nickname': '청취자'})
        self.assertNotIn('requester_nickname', engine.tracks.queue.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
