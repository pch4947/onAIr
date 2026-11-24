"""영상 제목과 채널 이름에서 곡 제목과 가수를 가려낸다."""
import unittest

from app.track_names import clean_channel, clean_names


class CleanNamesTests(unittest.TestCase):
    def check(self, title, channel, expected):
        self.assertEqual(clean_names(title, channel), expected)

    def test_artist_channel_and_video_words(self):
        self.check('Nick Drake - Pink Moon (Official Music Video)', 'NickDrakeVEVO', ('Pink Moon', 'Nick Drake'))
        self.check('Queen – Bohemian Rhapsody (Official Video Remastered)', 'Queen Official',
                   ('Bohemian Rhapsody', 'Queen'))
        self.check('Coldplay - Yellow (Official Video) [4K]', 'Coldplay', ('Yellow', 'Coldplay'))

    def test_topic_channel_title_is_already_the_song(self):
        self.check('Pink Moon', 'Nick Drake - Topic', ('Pink Moon', 'Nick Drake'))
        self.check('밤편지', '아이유 - Topic', ('밤편지', '아이유'))

    def test_song_first_order_is_recognised_by_the_channel(self):
        self.check('Yellow - Coldplay (Lyrics)', 'Coldplay', ('Yellow', 'Coldplay'))

    def test_lyric_and_label_channels_use_the_artist_in_the_title(self):
        self.check('The Weeknd - Blinding Lights (Lyrics)', '7clouds Lyrics', ('Blinding Lights', 'The Weeknd'))
        self.check('[MV] IU(아이유) _ Blueming(블루밍)', '1theK (원더케이)', ('Blueming(블루밍)', 'IU(아이유)'))
        self.check('아이유 (IU) - 밤편지 (Through the Night) [가사]', '가사 채널',
                   ('밤편지 (Through the Night)', '아이유 (IU)'))

    def test_quoted_song_title(self):
        self.check("BTS (방탄소년단) 'Dynamite' Official MV", 'HYBE LABELS', ('Dynamite', 'BTS (방탄소년단)'))
        self.check("Girls' Generation 소녀시대 'Gee' MV", 'SMTOWN', ('Gee', "Girls' Generation 소녀시대"))
        # 따옴표로 묶은 말이 둘이면 어느 것이 곡인지 모른다
        title = "Europe 'The Final Countdown' - From 'Live At Sweden Rock - 30 Anniversary Show'"
        self.check(title, 'earMUSIC', (title, 'earMUSIC'))

    def test_unsure_titles_are_not_split(self):
        # 가수 채널인데 어느 쪽도 채널 이름이 아니다 — 부제일 수 있다
        self.check('밤편지 - Through the Night', '아이유', ('밤편지 - Through the Night', '아이유'))
        # 올린 사람이 누구인지 모른다
        self.check('Queen - Bohemian Rhapsody', 'john1985', ('Queen - Bohemian Rhapsody', 'john1985'))
        # 구분자가 둘 이상이다
        self.check('A - B - C', 'A', ('A - B - C', 'A'))

    def test_words_that_say_which_recording_are_kept(self):
        self.check('Queen - Bohemian Rhapsody (Live Aid 1985)', 'Queen Official',
                   ('Bohemian Rhapsody (Live Aid 1985)', 'Queen'))
        self.check('AC/DC - Thunderstruck (Live At River Plate)', 'acdcVEVO',
                   ('Thunderstruck (Live At River Plate)', 'AC/DC'))

    def test_song_titles_that_end_in_a_video_word_survive(self):
        self.check('India.Arie - Video', 'IndiaArieVEVO', ('Video', 'India.Arie'))
        self.check('Lana Del Rey - Video Games', 'Lana Del Rey', ('Video Games', 'Lana Del Rey'))

    def test_short_channel_name_does_not_match_inside_a_word(self):
        self.check('Genius - IU', 'IU', ('Genius', 'IU'))

    def test_nothing_left_keeps_the_original(self):
        self.check('(Official Video)', 'someone', ('(Official Video)', 'someone'))
        self.check('노래', '', ('노래', ''))

    def test_channel(self):
        self.assertEqual(clean_channel('TaylorSwiftVEVO'), 'Taylor Swift')
        self.assertEqual(clean_channel('BLACKPINK'), 'BLACKPINK')
        self.assertEqual(clean_channel('IU Official'), 'IU')
        self.assertEqual(clean_channel('VEVO'), 'VEVO')


if __name__ == '__main__':
    unittest.main()
