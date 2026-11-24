"""DJ 선곡 — 방의 장르와 오늘의 주제에 맞는 곡을 DJ가 고른다 (계약 3.4절 track.suggest, #295).

사람이 플레이리스트를 채우지 않으면 곡이 없어 DJ 멘트만 이어진다(2026-11-15 실제 방송). 곡마다 장르를 붙여
두는 방법이 없어 백엔드의 자동 선곡은 만들지 못했다(계약 9.3절 3번). 그래서 곡 후보는 엔진이 LLM으로 내고,
백엔드가 YouTube에서 찾아 대기열에 넣는다.

LLM은 없는 곡을 지어낼 수 있다. 여기서 낸 제목·아티스트는 **검색어**일 뿐이고, 방송에서 소개하는 것은
백엔드가 실제로 찾은 곡의 정보다(track.queued의 track).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ..domain import Prompt, StationProfile
from ..pipeline.llm import LlmError
from ..prompt_data import DATA_RULES, GENRES, PLAYED, STATION_CONCEPT, TOPIC, data_block

log = logging.getLogger(__name__)

MARKER = "방송에 틀 곡 고르기"  # 더미 LLM이 이 요청을 알아보는 구절 — 지우거나 다른 지시에 쓰지 않는다
MAX_TITLE_CHARS = 80
MAX_ARTIST_CHARS = 60
MAX_WHY_CHARS = 100
MAX_PLAYED = 30  # 프롬프트에 넣는 이미 튼 곡 수 — 최근 것부터

# 장르를 여럿 고른 방 — 장르를 한꺼번에 주면 LLM이 섞어 읽는다(J-POP, 발라드, 인디 → 일본 인디 발라드만).
# 그래서 차례마다 한 장르만 준다. 이미 튼 곡도 끌어당긴다 — 앞 차례에 일본 곡을 틀었으면 발라드 차례에도
# 일본 곡을 고른다. 그 목록을 따라가지 말라고 적는다 (ENGINE_TUNING_LOG T-022)
TURN_RULE = ("- 이 방송은 여러 장르를 차례로 튼다. 이번에는 아래 선곡 취향의 장르에서만 고른다. "
             "이미 튼 곡에는 다른 장르 차례에 튼 곡이 섞여 있다. 그 목록은 같은 곡을 피하는 데만 쓰고, "
             "그 곡들의 장르나 나라를 따라가지 않는다. "
             "그 안에서 오늘의 화제나 분위기와 어울리는 곡이면 더 좋다.")

_SYSTEM = ("너는 라디오 방송의 선곡 담당이다. 방송의 분위기에 맞는, 실제로 발매된 곡을 고른다.\n"
           + DATA_RULES)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_COUNT_RE = re.compile(r"곡 후보 (\d+)개")
_KEY_RE = re.compile(r"[\W_]")  # 글자와 숫자만 남긴다 — 한글·영문만 남기면 일본어 제목은 열쇠가 비어 곡이 버려진다


@dataclass(frozen=True)
class Pick:
    title: str
    artist: str
    why: str = ""  # 이 방송에 고른 까닭 한 줄 — 곡 소개 멘트의 소재

    @property
    def label(self) -> str:
        return f"{self.title} — {self.artist}" if self.artist else self.title

    @property
    def key(self) -> str:
        return song_key(self.title)


def song_key(title: str) -> str:
    """같은 곡을 가리는 열쇠 — 띄어쓰기·기호·대소문자를 뺀 제목. 괄호 안(feat., 버전 표기)은 보지 않는다."""
    return _KEY_RE.sub("", re.sub(r"[(\[].*?[)\]]", "", title).lower())


# 다른 사람이 부른 것, 공연 실황처럼 제안한 음원이 아님을 알리는 말. 이런 영상은 올린 사람의 이름이 맞는 소개일 수 있다
_OTHER_RECORDING = re.compile(r"(?i)\b(cover|live|remix|inst|instrumental|karaoke|mr)\b|커버|라이브|리믹스|노래방")
_SHORT_NAME = 2  # 이 길이까지의 이름은 낱말째 같아야 한다 — "IU"가 "premium" 안에서 걸리지 않게


def _named(name: str, found: str) -> bool:
    """found(영상 제목과 채널 이름)에 name이 들어 있는가. 띄어쓰기·기호·대소문자는 보지 않는다."""
    key = song_key(name)
    if not key:
        return False
    if len(key) <= _SHORT_NAME:
        return key in re.sub(r"[\W_]+", " ", found.lower()).split()
    return key in _KEY_RE.sub("", found.lower())


def confirmed_names(pick: Pick, track: dict) -> tuple[str, str] | None:
    """찾은 곡이 제안한 곡이 맞으면 제안한 (제목, 가수). 확인할 수 없으면 None — 받은 곡 정보로 소개한다.

    가사 영상을 올리는 채널의 곡은 제목이 "가수 - 곡", 가수가 채널 이름으로 온다("김광석 - 바람이 불어오는 곳" / "웅키").
    그대로 소개하면 DJ가 "웅키의 …"라고 한다(#385). 제안한 제목과 가수가 영상 제목·채널 이름에 모두 들어 있으면
    같은 곡이므로 엔진이 아는 이름을 쓴다. 하나라도 없으면 검색 결과가 다른 곡일 수 있어 건드리지 않는다.
    """
    title, artist = str(track.get("title") or ""), str(track.get("artist") or "")
    if _OTHER_RECORDING.search(title) and not _OTHER_RECORDING.search(pick.title):
        return None
    found = f"{title} {artist}"
    if _named(pick.title, found) and _named(pick.artist, found):
        return pick.title, pick.artist
    return None


def build_prompt(profile: StationProfile, genres: list[str], topic: str | None, played: list[str],
                 count: int, *, turn_of_many: bool = False) -> Prompt:
    """turn_of_many: 방의 장르가 여럿이고 genres는 그 가운데 이번 차례의 것이다 (SongPicker.genres_for_turn)."""
    lines = [
        f"{MARKER}: 이 방송에서 다음에 틀 곡 후보 {count}개를 고른다.",
        "- 실제로 발매된 곡만 고르고, 제목과 아티스트를 정확히 적는다. 확실하지 않은 곡은 넣지 않는다.",
        (TURN_RULE if turn_of_many else
         "- 아래 선곡 취향의 장르에서 고른다. 그 안에서 오늘의 화제나 분위기와 어울리는 곡이면 더 좋다."
         if genres else "- 아래 스테이션 컨셉에 어울리는 곡을 고른다. 오늘의 화제와 어울리면 더 좋다."),
        "- 이미 튼 곡은 다시 고르지 않고, 바로 앞에 튼 곡과 같은 아티스트도 피한다. 후보끼리도 아티스트가 겹치지 않게 한다.",
        "- 누구나 아는 곡만 고르지 않는다. 라이브 실황, 리믹스, 모음집, 10분이 넘는 곡은 넣지 않는다.",
        ("- 곡마다 이 방송에 고른 까닭(why)을 한 문장으로 붙인다. 곡의 분위기와 지금 방송이 어울리는 점을 쓰고, "
         "수상·발매 시기·일화처럼 확인할 수 없는 사실은 쓰지 않는다."),
        '- 출력은 JSON 하나만: {"songs": [{"title": "곡 제목", "artist": "아티스트", "why": "까닭"}]}',
        "",
        data_block(STATION_CONCEPT, profile.concept),
    ]
    if genres:
        lines.append(data_block(GENRES, ", ".join(genres)))
    if topic:
        lines.append(data_block(TOPIC, topic))
    if played:
        lines.append(data_block(PLAYED, "\n".join(f"- {p}" for p in played[-MAX_PLAYED:])))
    return Prompt(system=_SYSTEM, user="\n".join(lines))


def parse_picks(text: str) -> list[Pick]:
    """LLM 출력의 곡 후보. 형식이 틀린 것은 버리고, 하나도 없으면 ValueError."""
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("JSON이 없습니다")
    data = json.loads(match.group(0))
    raw = data.get("songs") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("songs 배열이 없습니다")  # noqa: TRY004 — 호출자는 ValueError 하나로 받는다
    out: list[Pick] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            continue
        title = item["title"].strip()[:MAX_TITLE_CHARS]
        artist = item["artist"].strip()[:MAX_ARTIST_CHARS] if isinstance(item.get("artist"), str) else ""
        why = item["why"].strip()[:MAX_WHY_CHARS] if isinstance(item.get("why"), str) else ""
        key = song_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(Pick(title, artist, why))
    if not out:
        raise ValueError("쓸 수 있는 곡이 없습니다")
    return out


_DUMMY_SONGS = [
    ("첫눈 오는 밤", "가상의 가수", "조용한 밤과 잘 어울려서요."),
    ("새벽 버스", "모의 밴드", "창밖을 보며 듣기 좋아서요."),
    ("따뜻한 차 한 잔", "시뮬레이션 트리오", "잠깐 쉬어 가기 좋은 곡이라서요."),
    ("골목 끝 가로등", "더미 싱어즈", "하루를 마무리하는 분위기라서요."),
    ("느린 걸음", "테스트 듀오", "서두르지 않는 마음과 닮아서요."),
    ("주말의 창가", "예시 콰르텟", "편안하게 듣기 좋아서요."),
    ("비 갠 오후", "샘플 유닛", "가볍게 기분을 바꿔 줘서요."),
    ("늦은 답장", "모형 오케스트라", "차분한 이야기 뒤에 어울려서요."),
]


def dummy_picks_json(prompt: Prompt) -> str:
    """더미 LLM 응답 — 고정 목록에서 아직 튼 적 없는 곡. API 키 없이도 선곡이 돈다."""
    match = _COUNT_RE.search(prompt.user)
    count = int(match.group(1)) if match else 3
    fresh = [song for song in _DUMMY_SONGS if song[0] not in prompt.user] or list(_DUMMY_SONGS)
    return json.dumps({"songs": [{"title": t, "artist": a, "why": w} for t, a, w in fresh[:count]]},
                      ensure_ascii=False)


class SongPicker:
    """방송 하나의 선곡. 같은 곡을 두 번 제안하지 않는다 — 못 찾은 곡을 되풀이해 찾지 않게 실패한 것도 기억한다."""

    def __init__(self, llm, profile: StationProfile, genres: list[str], topic: str | None, *,
                 violates, count: int = 3, max_tokens: int = 2000):
        self._llm = llm
        self.profile = profile
        self.genres = list(genres)
        self.topic = topic
        self._violates = violates  # 까닭은 방송에 나갈 말의 재료다 — 대본과 같은 L2 규칙을 본다
        self.count = count
        self.max_tokens = max_tokens
        self._suggested: dict[str, str] = {}  # 곡 열쇠 → 제안한 곡 이름
        self._turn = 0  # 고른 횟수 — 장르가 여럿이면 이 순서로 돌아가며 고른다

    def use_persona(self, profile: StationProfile, genres: list[str]) -> None:
        """방송 중에 DJ가 바뀌었다 (#261) — 새 DJ의 컨셉과 선곡 취향으로 고른다."""
        self.profile = profile
        self.genres = list(genres)

    def genres_for_turn(self) -> list[str]:
        """이번 차례에 고를 장르. 장르가 여럿이면 하나씩 돌아가며 — 백엔드는 후보 가운데 먼저 찾은 한 곡만 튼다."""
        if len(self.genres) < 2:
            return self.genres
        return [self.genres[self._turn % len(self.genres)]]

    async def pick(self, played: list[str]) -> list[Pick]:
        """곡 후보. 제공자 장애나 해석 실패면 빈 목록 — 다음 차례에 다시 고른다."""
        heard = {song_key(p.split(" — ")[0]) for p in played}
        # 제안했지만 틀지 못한 곡도 알려 준다 — 알려 주지 않으면 같은 곡을 다시 낸다
        known = [*played, *(label for key, label in self._suggested.items() if key not in heard)]
        genres = self.genres_for_turn()
        self._turn += 1  # 실패해도 넘어간다 — 한 장르에서 곡을 못 내도 다음 장르로 이어 가게
        try:
            text = await self._llm.complete(
                build_prompt(self.profile, genres, self.topic, known, self.count,
                             turn_of_many=len(self.genres) > 1),
                max_tokens=self.max_tokens)
            picks = parse_picks(text)
        except (LlmError, ValueError, OSError) as exc:
            log.warning("선곡 실패: %s", exc)
            return []
        fresh = []
        for pick in picks:
            if pick.key in self._suggested or pick.key in heard:
                continue
            if self._violates(f"{pick.title} {pick.artist}") is not None:
                continue
            if pick.why and self._violates(pick.why) is not None:
                pick = Pick(pick.title, pick.artist)  # 곡은 쓰고 까닭만 버린다
            fresh.append(pick)
        fresh = fresh[:self.count]
        self._suggested.update((p.key, p.label) for p in fresh)
        return fresh
