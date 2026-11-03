"""채팅 반영 정책 — 분류, 송출 타임라인, 풀, 선별기, 정규화 (CORNER_DESIGN 5.1.3, 5.7, U15)."""
from pathlib import Path

import pytest

from onair_engine.domain import Anchor, ChatType, ListenerRequest
from onair_engine.inputs.classify import classify, keywords
from onair_engine.inputs.on_air import OnAirTimeline
from onair_engine.inputs.pool import QuotePool
from onair_engine.inputs.rules import load_chat_rules
from onair_engine.inputs.selector import QuoteSelector
from onair_engine.pipeline.normalize import normalize_for_tts

RULES = load_chat_rules(Path(__file__).resolve().parents[1] / "config" / "chat_rules.yaml")
SONG = Anchor("track:1", "track", keywords("밤편지 아이유"), 0)


def _req(request_id="r1", body="안녕하세요 반가워요", who="u1", at=100.0,
         chat_type=ChatType.STANDALONE, anchor_id=None, anchor_seq=None):
    return ListenerRequest(request_id=request_id, kind="story", body=body, requester_ref=who,
                           received_at=at, chat_type=chat_type, anchor_id=anchor_id,
                           anchor_seq=anchor_seq)


def _selector(criterion="recency", threshold=8):
    return QuoteSelector(criterion, flood_threshold=threshold, max_quote_chars=60,
                         violates=lambda text: "bad" if "금칙어" in text else None)


def _pool(track_end=lambda _: None):
    return QuotePool(reaction_window_sec=60, standalone_window_sec=600, track_end=track_end)


# ── 분류 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("body", "anchor", "chat_type", "rule"), [
    ("이 노래 제목이 뭐예요", None, ChatType.REACTION, "pointing"),
    ("아이유 목소리 진짜 맑아요", SONG, ChatType.REACTION, "anchor_keyword"),
    ("밤편지는 언제 들어도 설레요", SONG, ChatType.REACTION, "anchor_keyword"),
    ("대박 ㅋㅋ", None, ChatType.REACTION, "short_reaction"),
    ("저 오늘 시험 끝났어요", SONG, ChatType.STANDALONE, "default"),
])
def test_classify_rules(body, anchor, chat_type, rule):
    assert classify(body, anchor, RULES) == (chat_type, rule)


def test_rule_order_prefers_pointing_over_keyword():
    assert classify("방금 아이유 노래 좋네요", SONG, RULES)[1] == "pointing"


def test_long_message_with_reaction_word_is_not_short_reaction():
    body = "대박 오늘 회사에서 있었던 일을 길게 들려 드리고 싶은데 괜찮을까요"
    assert classify(body, None, RULES) == (ChatType.STANDALONE, "default")


def test_keywords_strip_josa_and_common_words():
    assert keywords("아이유는 오늘 밤편지를") == frozenset({"아이유", "밤편지"})


# ── 송출 타임라인 ─────────────────────────────────────────────────────

def _anchor(timeline, name="topic"):
    return timeline.new_anchor(name, "topic", ())


def test_current_follows_estimated_position_including_song():
    t = OnAirTimeline()
    a, b = _anchor(t, "a"), _anchor(t, "b")
    t.append("s1", a, 10_000, now=0.0, track_ref="yt:1", track_ms=100_000)
    t.append("s2", b, 10_000, now=1.0)
    assert t.current(50.0) == a  # 곡 재생 중이면 곡 소개와 같은 앵커
    assert t.current(111.0) == b
    assert t.track_end("a") == 110.0


def test_sync_started_is_idempotent_and_moves_later_entries():
    t = OnAirTimeline()
    a, b = _anchor(t, "a"), _anchor(t, "b")
    t.append("s1", a, 10_000, now=0.0)
    t.append("s2", b, 10_000, now=0.0)
    assert t.sync_started("s1", 30.0)
    assert t.current(35.0) == a
    assert t.current(41.0) == b  # 앞 세그먼트가 늦었으니 뒤도 늦다
    assert not t.sync_started("s1", 99.0)  # 재전달 — 처음 값 유지
    assert t.current(41.0) == b
    assert not t.sync_started("unknown", 1.0)


def test_sync_track_moves_song_end():
    t = OnAirTimeline()
    a = _anchor(t, "a")
    t.append("s1", a, 10_000, now=0.0, track_ref="yt:1", track_ms=100_000)
    t.sync_track("yt:1", 20.0)
    assert t.track_end("a") == 120.0


def test_empty_timeline_has_no_anchor():
    assert OnAirTimeline().current(0.0) is None


# ── 풀: 창과 앵커 유효성 ──────────────────────────────────────────────

def test_windows_differ_by_chat_type():
    pool = _pool()
    reaction = _req("react", chat_type=ChatType.REACTION)
    standalone = _req("alone", chat_type=ChatType.STANDALONE)
    pool.add(reaction)
    pool.add(standalone)
    assert pool.expire(159.0, None) == []
    assert pool.expire(161.0, None) == [reaction]
    assert pool.expire(699.0, None) == []
    assert pool.expire(701.0, None) == [standalone]


def test_reaction_window_starts_when_anchor_song_ends():
    pool = _pool(track_end=lambda anchor_id: 300.0 if anchor_id == "track:1" else None)
    req = _req(chat_type=ChatType.REACTION, anchor_id="track:1", anchor_seq=3)
    pool.add(req)
    assert pool.expire(290.0, 3) == []  # 도착 후 190초지만 곡이 아직 안 끝났다
    assert pool.expire(359.0, 3) == []
    assert pool.expire(361.0, 3) == [req]


def test_reaction_expires_when_anchor_is_two_behind():
    pool = _pool()
    req = _req(chat_type=ChatType.REACTION, anchor_id="a", anchor_seq=3)
    pool.add(req)
    assert pool.expire(101.0, 4) == []  # 바로 앞 앵커
    assert pool.expire(101.0, 5) == [req]


def test_standalone_ignores_anchor_distance():
    pool = _pool()
    pool.add(_req(chat_type=ChatType.STANDALONE, anchor_id="a", anchor_seq=0))
    assert pool.expire(101.0, 50) == []


def test_recent_arrivals_counts_only_window_and_put_back_does_not_recount():
    pool = _pool()
    first = _req("a", at=100.0)
    pool.add(first)
    pool.add(_req("b", at=150.0))
    assert pool.recent_arrivals(155.0) == 2
    assert pool.recent_arrivals(165.0) == 1
    pool.remove(first)
    pool.put_back(first)
    assert pool.items[0] is first
    assert pool.recent_arrivals(165.0) == 1


# ── 선별기 ───────────────────────────────────────────────────────────

def _select(selector, items, arrivals=2, last=None, words=frozenset(), limit=1):
    return selector.select(items, limit=limit, arrivals=arrivals, last_requester=last,
                           topic_words=words)


def test_criteria_pick_differently():
    old_on_topic = _req("old", body="밤편지 정말 좋아요", who="a", at=100.0)
    new_off_topic = _req("new", body="저녁 뭐 먹을까요", who="b", at=110.0)
    items = [old_on_topic, new_off_topic]
    words = keywords("밤편지")
    assert _select(_selector("recency"), items, words=words).picked == [new_off_topic]
    assert _select(_selector("relevance"), items, words=words).picked == [old_on_topic]


def test_diversity_prefers_people_not_yet_quoted():
    selector = _selector("diversity")
    assert _select(selector, [_req("a1", who="a", at=1.0)]).picked[0].requester_ref == "a"
    items = [_req("a2", who="a", at=9.0), _req("b1", who="b", at=2.0)]
    assert _select(selector, items).picked[0].requester_ref == "b"


def test_forget_restores_diversity_score():
    selector = _selector("diversity")
    first = _req("a1", who="a", at=1.0)
    _select(selector, [first])
    selector.forget(first)
    items = [_req("a2", who="a", at=9.0), _req("b1", who="b", at=2.0)]
    assert _select(selector, items).picked[0].requester_ref == "a"


def test_chat_type_does_not_affect_score():
    """유형을 점수에 넣으면 최근성 조건까지 관련성 우선처럼 된다 (5.7절)."""
    newer = _req("new", who="a", at=110.0, chat_type=ChatType.STANDALONE)
    older = _req("old", who="b", at=100.0, chat_type=ChatType.REACTION)
    assert _select(_selector("recency"), [older, newer]).picked == [newer]


def test_flood_allows_only_reactions():
    standalone = _req("alone", who="a", at=110.0, chat_type=ChatType.STANDALONE)
    reaction = _req("react", who="b", at=100.0, chat_type=ChatType.REACTION)
    result = _select(_selector(threshold=8), [standalone, reaction], arrivals=8)
    assert result.situation == "flood" and result.picked == [reaction]
    assert [c["request_id"] for c in result.candidates] == ["react"]


def test_situations():
    selector = _selector(threshold=8)
    assert (selector.situation(0), selector.situation(7), selector.situation(8)) == (
        "quiet", "normal", "flood")


@pytest.mark.parametrize("request_args", [
    {"who": "last"},  # 직전 인용과 같은 사람
    {"body": "가" * 61},  # 말로 인용하기에 너무 길다
    {"body": "ㅋㅋㅋㅋ"},  # 인용할 글자가 없다
    {"body": "이건 금칙어 입니다"},  # L2
])
def test_ineligible_inputs(request_args):
    assert _select(_selector(), [_req(**request_args)], last="last").picked == []


def test_same_text_counts_once():
    a = _req("a", body="노래 좋아요", who="a", at=2.0)
    b = _req("b", body="노래  좋아요", who="b", at=1.0)
    c = _req("c", body="다른 얘기예요", who="c", at=0.5)
    assert _select(_selector(), [a, b, c], limit=2).picked == [a, c]


def test_unknown_criterion_fails_at_startup():
    with pytest.raises(ValueError, match="선별 기준"):
        _selector("loudest")


# ── 정규화 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(("text", "expected"), [
    ("버스 놓쳤어요 ㅋㅋㅋㅋㅋ", "버스 놓쳤어요 크크크"),
    ("ㅋㅋ 좋아요", "크크 좋아요"),
    ("슬퍼요 ㅠㅠㅠㅠ", "슬퍼요 유유유"),
    ("아쉬워요 ㅜㅜ", "아쉬워요 우우"),
    ("ㅎㅎㅎ 네", "흐흐 네"),
    ("ㅊㅋ 합격", "축하 합격"),
    ("ㅇㅇ 맞아요", "응응 맞아요"),
    ("ㄷㄷㄷ 대단해요", "덜덜 대단해요"),
    ("ㄱㄱ 가요", "고고 가요"),
    ("좋아요 😀 ❤️", "좋아요"),
    ("ㅏ 그렇군요", "그렇군요"),
    ("그대로인 문장입니다.", "그대로인 문장입니다."),
])
def test_normalize(text, expected):
    assert normalize_for_tts(text) == expected


def test_normalize_keeps_original_when_nothing_left():
    assert normalize_for_tts("😀") == "😀"
