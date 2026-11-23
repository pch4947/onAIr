"""YouTube 영상 제목과 채널 이름에서 곡 제목과 가수를 가려낸다.

영상 제목은 "Nick Drake - Pink Moon (Official Music Video)", 채널은 "NickDrakeVEVO"처럼 온다. 그대로 넘기면
화면에 그대로 보이고 DJ도 그대로 읽는다. 여기서 영상 종류를 알리는 말을 빼고 "가수 - 제목"을 나눈다.

확실하지 않으면 건드리지 않는다 — 잘못 나누면 없는 가수 이름이 방송에 나간다.
"""
import re

# 괄호 안이 이 말들과 숫자뿐이면 괄호째 뺀다. "Live at Wembley"처럼 곡을 가리키는 말이 섞인 괄호는 남긴다
_VIDEO_WORDS = frozenset((
    'official', 'music', 'video', 'audio', 'mv', 'm', 'v', 'lyric', 'lyrics', 'visualizer', 'visualiser',
    'hd', 'hq', '4k', 'uhd', 'full', 'clip', 'performance', 'teaser', 'color', 'coded', 'eng', 'han', 'rom', 'sub',
    'remaster', 'remastered', 'upgrade', 'upgraded', 'original',
    '뮤직비디오', '뮤비', '공식', '가사', '음원', '고음질', '자막', '한글', '영상'))
_BRACKET = re.compile(r'\(([^()]*)\)|\[([^\[\]]*)\]|【([^【】]*)】|《([^《》]*)》|〈([^〈〉]*)〉')
_WORD = re.compile(r'[0-9A-Za-z]+|[가-힣]+')
# 괄호 없이 끝에 붙는 말: "... Official Music Video", "... | Official Audio", "... MV".
# 그냥 "Video"나 "Audio"로 끝나는 것은 곡 제목일 수 있어 건드리지 않는다
_TAIL = re.compile(r'(?i)[\s|:\-–—]*\b(official\s+((music|lyrics?)\s+)?(video|audio|m/?v|visuali[sz]er)'
                   r'|music\s+video|lyrics?\s+video|m/?v)\s*$')
# K-pop 영상 제목: 가수 '곡' — "BTS (방탄소년단) 'Dynamite'"
_QUOTED = re.compile(r'''^(.+?)\s+['‘"“]([^'‘’"“”]+)['’"”]$''')
_SEPARATOR = re.compile(r'\s+[-–—_]\s+')
_CHANNEL_TAIL = re.compile(r'(?i)(\s*-\s*topic|\s*vevo|\s+official(\s+(channel|youtube))?|\s*공식(\s*채널)?)\s*$')
_CAMEL = re.compile(r'(?<=[a-z])(?=[A-Z])')
# 가수가 아니라 곡을 모아 올리는 채널
_COLLECTOR = re.compile(r'(?i)lyrics?|가사|music|records?|entertainment|labels?|1thek|stone music|genie|melon'
                        r'|sounds?|playlist|플레이리스트|음악|smtown|jyp|yg ')


def _video_only(text):
    words = _WORD.findall(text.lower())
    return bool(words) and all(word in _VIDEO_WORDS or word.isdigit() for word in words)


def _strip_video_words(title):
    def bracket(match):
        inner = next(group for group in match.groups() if group is not None)
        return ' ' if _video_only(inner) or not inner.strip() else match.group(0)
    cleaned = _BRACKET.sub(bracket, title)
    previous = None
    while cleaned != previous:  # "Song Official Video MV"처럼 겹쳐 붙은 것까지
        previous, cleaned = cleaned, _TAIL.sub('', cleaned)
    return re.sub(r'\s{2,}', ' ', cleaned).strip(' -–—_|:')


def _key(text):
    return ''.join(_WORD.findall(text.lower()))


def clean_channel(channel):
    """채널 이름에서 가수 이름을 얻는다: "NickDrakeVEVO" → "Nick Drake", "아이유 - Topic" → "아이유"."""
    name, previous = channel.strip(), None
    vevo = bool(re.search(r'(?i)vevo\s*$', name))
    while name != previous:
        previous, name = name, _CHANNEL_TAIL.sub('', name).strip()
    if vevo and ' ' not in name:
        name = _CAMEL.sub(' ', name)
    return name or channel.strip()


def _same_name(part, channel_key):
    """제목의 한쪽이 채널(가수) 이름인가. 괄호로 덧붙인 이름("IU(아이유)")은 괄호 안팎 어느 쪽이 같아도 된다."""
    for key in {_key(part)} | {_key(piece) for piece in re.split(r'[()]', part)}:
        if key and channel_key and (key == channel_key or (
                min(len(key), len(channel_key)) >= 4 and (key in channel_key or channel_key in key))):
            return True
    return False


def clean_names(title, channel):
    """(곡 제목, 가수). 가려낼 수 없으면 영상 종류를 알리는 말만 뺀 제목과 다듬은 채널 이름을 돌려준다."""
    stripped = _strip_video_words(title) or title.strip()
    artist = clean_channel(channel)
    quoted = _QUOTED.match(stripped)
    if quoted:
        return quoted.group(2).strip(), quoted.group(1).strip()
    parts = [part.strip() for part in _SEPARATOR.split(stripped)]
    if len(parts) != 2 or not all(parts):
        return stripped, artist
    left, right = parts
    channel_key = _key(artist)
    if _same_name(right, channel_key) and not _same_name(left, channel_key):
        return left, right          # "곡 - 가수"
    if _same_name(left, channel_key) or not channel_key or _COLLECTOR.search(channel):
        return right, left          # "가수 - 곡". 가사·음원 채널의 제목도 이 순서다
    return stripped, artist         # 채널이 어느 쪽과도 다르다 — 나누지 않는다
