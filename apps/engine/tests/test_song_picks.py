"""DJ 선곡 — 들고 있는 다음 곡이 없으면 DJ가 곡 후보를 내고 백엔드가 찾아 튼다 (계약 3.4절 track.suggest, #295)."""
import asyncio
import json
import time
from pathlib import Path

import pytest

import onair_engine.engine as engine_module
from onair_engine.corners import build_prompt
from onair_engine.corners.run import TrackSource
from onair_engine.corners.steps import ANONYMOUS, ANONYMOUS_REQUEST_RULE, track_line, upload_channel
from onair_engine.domain import Material, Prompt, StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.inputs.song_picks import (
    MARKER,
    TURN_RULE,
    Pick,
    SongPicker,
    confirmed_names,
    dummy_picks_json,
    parse_picks,
    song_key,
)
from onair_engine.inputs.song_picks import build_prompt as pick_prompt
from onair_engine.main import load_config
from onair_engine.manager import EngineManager
from onair_engine.pipeline.llm import DummyLlmClient
from onair_engine.prompt_data import GENRES, PICK_WHY, PLAYED
from test_corner_skip import _summaries, _until

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
PROFILE = StationProfile(dj_name="새벽", tone="차분한 존댓말", concept="심야 스터디 라디오")
SONG = {"track_ref": "yt:a", "title": "밤편지", "artist": "아이유", "duration_ms": 200_000}


# ── 곡 고르기 ──────────────────────────────────────────────────────────

def test_prompt_carries_genres_topic_and_played_songs_as_data():
    prompt = pick_prompt(PROFILE, ["발라드", "인디"], "수능 응원", ["밤편지 — 아이유"], 3)
    assert MARKER in prompt.user and "곡 후보 3개" in prompt.user
    assert f'<data label="{GENRES}">\n발라드, 인디\n</data>' in prompt.user
    assert f'<data label="{PLAYED}">\n- 밤편지 — 아이유\n</data>' in prompt.user
    bare = pick_prompt(PROFILE, [], None, [], 3).user
    assert f'label="{GENRES}"' not in bare and f'label="{PLAYED}"' not in bare
    assert "스테이션 컨셉에 어울리는" in bare


def test_parse_keeps_wellformed_songs_and_drops_duplicates():
    text = json.dumps({"songs": [
        {"title": "밤편지", "artist": "아이유", "why": "차분해서요."},
        {"title": "밤 편지 (Live)", "artist": "아이유"},  # 같은 곡
        {"artist": "제목 없음"}, "문자열", {"title": "  "},
        {"title": "오르트구름", "artist": 3},
    ]}, ensure_ascii=False)
    assert parse_picks(f"고른 곡입니다.\n{text}") == [Pick("밤편지", "아이유", "차분해서요."), Pick("오르트구름", "")]
    for bad in ("곡이 없어요", '{"songs": "밤편지"}', '{"songs": [{"artist": "아이유"}]}'):
        with pytest.raises(ValueError):
            parse_picks(bad)
    assert song_key("밤 편지 (feat. 누구)") == song_key("밤편지")
    # 한글·영문이 아닌 제목도 곡이다 — 열쇠가 비면 버려져서 일본 곡은 하나도 제안하지 못했다
    japanese = json.dumps({"songs": [{"title": "夜に駆ける", "artist": "YOASOBI"}]}, ensure_ascii=False)
    assert parse_picks(japanese) == [Pick("夜に駆ける", "YOASOBI")]
    assert song_key("夜に 駆ける!") == song_key("夜に駆ける") != song_key("群青")


def test_picker_never_suggests_a_song_twice_nor_one_already_played():
    picker = SongPicker(DummyLlmClient(delay_sec=0), PROFILE, ["발라드"], "수능 응원",
                        violates=lambda text: None, count=3)

    async def scenario():
        first = await picker.pick([])
        second = await picker.pick([first[0].label])
        return first, second

    first, second = asyncio.run(scenario())
    assert len(first) == 3 and len(second) == 3
    assert not {p.key for p in first} & {p.key for p in second}


def test_picker_takes_one_genre_per_turn_when_the_room_has_several():
    """장르를 한꺼번에 주면 LLM이 섞어 읽어 한쪽 곡만 낸다 — 차례마다 한 장르만 준다 (T-022)."""
    class Recording:
        def __init__(self):
            self.prompts = []

        async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
            self.prompts.append(prompt.user)
            return dummy_picks_json(prompt)

    def asked(llm):
        return [user.split(f'<data label="{GENRES}">\n')[1].split("\n</data>")[0] for user in llm.prompts]

    llm = Recording()
    picker = SongPicker(llm, PROFILE, ["J-POP", "발라드", "인디"], None, violates=lambda text: None)

    async def scenario():
        for _ in range(4):
            await picker.pick([])
        picker.use_persona(PROFILE, ["재즈"])  # DJ가 바뀌어 장르가 하나가 됐다
        await picker.pick([])

    asyncio.run(scenario())
    assert asked(llm) == ["J-POP", "발라드", "인디", "J-POP", "재즈"]
    assert all(TURN_RULE in user for user in llm.prompts[:4]) and TURN_RULE not in llm.prompts[4]


def test_picker_drops_unsafe_songs_and_unsafe_reasons_only():
    class Fixed:
        async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
            return json.dumps({"songs": [{"title": "나쁜 제목", "artist": "가수", "why": "좋아서요."},
                                         {"title": "좋은 곡", "artist": "가수", "why": "나쁜 까닭"},
                                         {"title": "평범한 곡", "artist": "가수", "why": "편안해서요."}]},
                              ensure_ascii=False)

    picker = SongPicker(Fixed(), PROFILE, [], None, violates=lambda text: "x" if "나쁜" in text else None)
    assert asyncio.run(picker.pick([])) == [Pick("좋은 곡", "가수"), Pick("평범한 곡", "가수", "편안해서요.")]


def test_dummy_llm_answers_the_pick_prompt():
    prompt = pick_prompt(PROFILE, ["발라드"], "주제", [], 2)
    assert len(parse_picks(dummy_picks_json(prompt))) == 2
    assert len(parse_picks(asyncio.run(DummyLlmClient(delay_sec=0).complete(prompt, max_tokens=100)))) == 2


def test_track_source_knows_whether_it_holds_a_song_and_when_one_was_taken():
    tracks = TrackSource(dummy=False)
    assert not tracks.has_queued and tracks.taken_at is None
    tracks.queue(SONG)
    assert tracks.has_queued
    assert tracks.take()["title"] == "밤편지" and not tracks.has_queued and tracks.taken_at is not None
    tracks.started()
    assert tracks.taken_at is None


# ── 곡 소개 ────────────────────────────────────────────────────────────

def test_intro_of_a_dj_pick_says_why_it_was_picked():
    plain = build_prompt("music_intro", Material(track=dict(SONG)), PROFILE).user
    picked = build_prompt("music_intro", Material(track={**SONG, "dj_pick": True, "pick_why": "차분해서요."}),
                          PROFILE).user
    assert "직접 고른 곡" not in plain and PICK_WHY not in plain
    assert "직접 고른 곡" in picked and f'<data label="{PICK_WHY}">\n차분해서요.\n</data>' in picked
    no_reason = build_prompt("music_intro", Material(track={**SONG, "dj_pick": True}), PROFILE).user
    assert "직접 고른 곡" in no_reason and PICK_WHY not in no_reason


# ── 엔진 ──────────────────────────────────────────────────────────────

class Backend:
    def __init__(self):
        self.stations: list[tuple[str, dict]] = []

    async def publish_segment(self, sub):
        pass

    async def notify_request_state(self, request_id, state, detail=None):
        pass

    async def notify_station(self, event_type, payload):
        self.stations.append((event_type, payload))

    def suggests(self) -> list[dict]:
        return [payload for kind, payload in self.stations if kind == "track.suggest"]


def _engine(tmp_path, backend: Backend, *, emit: bool = True, **config) -> StationEngine:
    cfg = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30, corners=["topic_talk"],
                        topic="수능 응원", music_taste=["발라드"], **config)
    return StationEngine(cfg, EngineSettings(
        audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite", safety_rules_path=RULES,
        dummy_tracks=False, emit_song_picks=emit, song_pick_grace_sec=0.0), backend)


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(engine_module, "_PICK_POLL_SEC", 0.01)


def test_engine_suggests_when_it_holds_no_song_and_introduces_what_the_backend_found(tmp_path):
    backend = Backend()
    engine = _engine(tmp_path, backend)

    async def scenario():
        task = asyncio.create_task(engine._pick_songs())
        await _until(lambda: backend.suggests(), "선곡 제안")
        await asyncio.sleep(0.05)
        assert len(backend.suggests()) == 1, "답이 오기 전에는 다시 제안하지 않는다"
        suggest = backend.suggests()[0]
        assert len(suggest["candidates"]) == 3 and set(suggest["candidates"][0]) == {"title", "artist"}
        # 백엔드가 둘째 후보를 찾아 넣었다 — 곡 정보는 검색 결과다
        engine.on_track_queued({"track": {**SONG, "title": "찾은 곡 (Official Audio)"}, "source": "dj",
                                "suggest_id": suggest["suggest_id"], "candidate": 1})
        await asyncio.sleep(0.05)
        assert len(backend.suggests()) == 1, "다음 곡을 들고 있으면 제안하지 않는다"
        task.cancel()

    asyncio.run(scenario())
    track = engine.tracks.take()
    assert track["title"] == "찾은 곡 (Official Audio)" and track["dj_pick"] is True
    assert track["pick_why"], "고른 까닭은 채택된 후보의 것을 쓴다"
    engine.telemetry.close()
    events = _summaries(tmp_path)
    assert any(e.startswith("선곡 제안 · ") and e.endswith("외 2곡") for e in events)
    assert "DJ 선곡 도착 · 찾은 곡 (Official Audio) - 아이유" in events


def test_confirmed_names_need_both_title_and_artist_in_what_was_found():
    """가사 영상을 올리는 채널의 곡은 가수 자리에 채널 이름이 온다 — DJ가 "웅키의 …"라고 소개했다 (#385)."""
    pick = Pick("바람이 불어오는 곳", "김광석")
    assert confirmed_names(pick, {"title": "김광석 - 바람이 불어오는 곳", "artist": "웅키"}) == ("바람이 불어오는 곳", "김광석")
    assert confirmed_names(Pick("밤편지", "아이유"), {"title": "밤편지 (Through the Night)", "artist": "아이유 (IU)"}) \
        == ("밤편지", "아이유")
    # 가수가 어디에도 없다 — 올린 사람이 부른 곡일 수 있다
    assert confirmed_names(Pick("숲", "최유리"), {"title": "숲", "artist": "웅키"}) is None
    # 다른 곡이다
    assert confirmed_names(Pick("찾는 곡", "아이유"), {"title": "찾은 곡 (Official Audio)", "artist": "아이유"}) is None
    # 짧은 이름은 낱말째 같아야 한다
    assert confirmed_names(Pick("숲", "최유리"), {"title": "숲속의 노래 - 최유리", "artist": "누군가"}) is None
    assert confirmed_names(Pick("Blueming", "IU"), {"title": "Blueming premium mix", "artist": "누군가"}) is None
    assert confirmed_names(Pick("Blueming", "IU"), {"title": "IU - Blueming", "artist": "1theK"}) == ("Blueming", "IU")
    # 다른 사람이 부른 것은 올린 사람의 이름이 맞는 소개일 수 있다. 제목에 그 말이 든 곡("Alive")은 걸리지 않는다
    assert confirmed_names(Pick("숲", "최유리"), {"title": "최유리 - 숲 (Cover by 웅키)", "artist": "웅키"}) is None
    assert confirmed_names(Pick("Alive", "Sia"), {"title": "Sia - Alive", "artist": "lyricsbox"}) == ("Alive", "Sia")


def test_engine_introduces_a_confirmed_pick_by_the_suggested_names(tmp_path):
    backend = Backend()
    engine = _engine(tmp_path, backend)

    async def scenario():
        task = asyncio.create_task(engine._pick_songs())
        await _until(lambda: backend.suggests(), "선곡 제안")
        suggest = backend.suggests()[0]
        wanted = suggest["candidates"][0]
        engine.on_track_queued({"track": {**SONG, "title": f"{wanted['artist']} - {wanted['title']}", "artist": "웅키"},
                                "source": "dj", "suggest_id": suggest["suggest_id"], "candidate": 0})
        task.cancel()
        return wanted

    wanted = asyncio.run(scenario())
    track = engine.tracks.take()
    assert (track["title"], track["artist"]) == (wanted["title"], wanted["artist"]), "제안한 이름으로 소개한다"
    assert track["found_artist"] == "웅키" and track["track_ref"] == SONG["track_ref"]
    assert engine._played[-1] == f"{wanted['title']} — {wanted['artist']}", "튼 곡 목록도 같은 이름이다"
    engine.telemetry.close()
    assert f"DJ 선곡 도착 · {wanted['title']} - {wanted['artist']}" in _summaries(tmp_path)


def test_requested_song_keeps_the_names_the_backend_sent(tmp_path):
    """청취자가 신청한 곡은 엔진이 가수를 모른다 — 받은 곡 정보 그대로다."""
    backend = Backend()
    engine = _engine(tmp_path, backend)
    engine.on_track_queued({"track": {**SONG, "title": "김광석 - 바람이 불어오는 곳", "artist": "웅키"},
                            "source": "request", "request_id": "rq_1"})
    track = engine.tracks.take()
    assert (track["title"], track["artist"]) == ("김광석 - 바람이 불어오는 곳", "웅키") and "found_title" not in track
    engine.telemetry.close()


def test_upload_channel_is_not_handed_over_as_the_artist():
    """익명 신청곡 열 번 중 다섯 번을 "익명으로 신청해 주신 웅키님의 곡"이라고 소개했다 (#385)."""
    lyric = {**SONG, "title": "김광석 - 바람이 불어오는 곳", "artist": "웅키"}
    assert upload_channel(lyric) == "웅키"
    assert track_line(lyric) == "곡: 김광석 - 바람이 불어오는 곳\n영상을 올린 채널(가수가 아닐 수 있다): 웅키"
    # 가수의 채널일 수도 있다 — 이름을 지우지 않고 무엇인지 알려 준다
    assert "영상을 올린 채널(가수가 아닐 수 있다): 아이유" in track_line({**SONG, "title": "밤편지 - Through the Night"})
    # 백엔드가 가려낸 곡, 제목에 가수가 있는 곡, 나뉘지 않는 제목은 지금 그대로다
    assert track_line(SONG) == "곡: 밤편지 — 아이유"
    assert track_line({**SONG, "title": "아이유 - 밤편지"}) == "곡: 아이유 - 밤편지 — 아이유"
    assert track_line({**SONG, "title": "A - B - C", "artist": "누군가"}) == "곡: A - B - C — 누군가"
    assert upload_channel({**SONG, "artist": ""}) is None


def test_anonymous_request_is_not_called_by_a_name():
    lyric = {**SONG, "title": "김광석 - 바람이 불어오는 곳", "artist": "웅키", "requester_comment": "퇴근길에 듣고 싶어요"}
    for step in ("music_intro", "closing_farewell"):
        hidden = build_prompt(step, Material(track={**lyric, "requester_nickname": ANONYMOUS}), PROFILE).user
        assert ANONYMOUS_REQUEST_RULE in hidden and "신청자:" not in hidden and "닉네임과 함께" not in hidden
        assert "한마디: 퇴근길에 듣고 싶어요" in hidden
        named = build_prompt(step, Material(track={**lyric, "requester_nickname": "밤고양이"}), PROFILE).user
        assert "신청자: 밤고양이" in named and "닉네임과 함께 소개한다" in named and ANONYMOUS_REQUEST_RULE not in named


def test_failed_suggestion_is_retried_with_other_songs_and_rests_after_repeated_failures(tmp_path):
    backend = Backend()
    engine = _engine(tmp_path, backend)

    async def scenario():
        task = asyncio.create_task(engine._pick_songs())
        seen: set[str] = set()
        for n in range(1, 3):
            await _until(lambda n=n: len(backend.suggests()) == n, f"{n}번째 제안")
            suggest = backend.suggests()[-1]
            titles = {c["title"] for c in suggest["candidates"]}
            assert not titles & seen, "못 찾은 곡을 다시 제안하지 않는다"
            seen |= titles
            engine.on_track_suggest_failed({"suggest_id": "sg_other", "reason": "not_found"})  # 남의 답은 무시
            engine.on_track_suggest_failed({"suggest_id": suggest["suggest_id"], "reason": "not_found"})
        await _until(lambda: len(backend.suggests()) == 3, "세 번째 제안")
        engine.on_track_suggest_failed({"suggest_id": backend.suggests()[-1]["suggest_id"], "reason": "unavailable"})
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(scenario())
    assert len(backend.suggests()) == 3 and engine._pick_after > time.time() + 60, "연달아 세 번 실패하면 쉰다"
    engine.telemetry.close()
    assert _summaries(tmp_path).count("선곡 실패 · not_found") == 2


def test_engine_waits_while_the_backend_may_still_announce_a_song(tmp_path):
    backend = Backend()
    engine = _engine(tmp_path, backend)
    now = time.time()
    assert engine._needs_pick(now)
    engine.on_track_queued({"track": dict(SONG), "source": "playlist"})
    assert not engine._needs_pick(now), "플레이리스트 곡을 들고 있다"
    engine.tracks.take()
    assert not engine._needs_pick(time.time()), "소개한 곡이 시작되면 백엔드가 그다음 곡을 알린다 — 그때까지 기다린다"
    engine.on_track_started({"track_ref": "yt:a", "started_at": time.time()})
    assert engine._needs_pick(time.time() + 0.01), "곡이 시작됐는데 다음 곡이 없다"
    engine.on_track_suggest_failed({"suggest_id": None, "reason": "not_needed"})
    engine._pending_pick = ("sg_1", [Pick("곡", "가수")], time.time())
    assert not engine._needs_pick(time.time()), "답을 기다리는 제안이 있다"
    engine.on_track_suggest_failed({"suggest_id": "sg_1", "reason": "not_needed"})
    assert not engine._needs_pick(time.time() + 30) and engine._needs_pick(time.time() + 61), \
        "백엔드에 틀 곡이 있다고 했다 — 한동안 제안하지 않는다"
    engine._pending_pick = ("sg_2", [Pick("곡", "가수")], time.time() - 301)
    assert engine._needs_pick(time.time() + 61) and engine._pending_pick is None, "답이 없으면 다시 제안한다"
    engine.telemetry.close()
    assert "선곡 실패 · timeout" in _summaries(tmp_path)


def test_song_picks_are_off_by_default_and_first_song_counts_as_played(tmp_path):
    off = _engine(tmp_path / "off", Backend(), emit=False)
    assert off.song_picker is None
    off.telemetry.close()
    on = _engine(tmp_path / "on", Backend(), first_song=dict(SONG))
    assert on.song_picker is not None and on._played == ["밤편지 — 아이유"]
    on.telemetry.close()


def test_production_config_turns_song_picks_on():
    """운영 설정은 DJ 선곡을 켠다 — 백엔드가 track.suggest를 받게 된 뒤에 켰다 (#295). 설정에 없으면 꺼짐이다."""
    root = Path(__file__).resolve().parent.parent
    assert load_config(root / "config" / "station.example.yaml")[1].emit_song_picks is True
    assert EngineSettings().emit_song_picks is False


def test_manager_routes_suggest_failures(tmp_path):
    engine = _engine(tmp_path, Backend())
    engine._pending_pick = ("sg_1", [Pick("곡", "가수")], time.time())
    asyncio.run(EngineManager(EngineSettings())._route_station_event(
        engine, {"type": "track.suggest_failed", "payload": {"suggest_id": "sg_1", "reason": "busy"}}))
    assert engine._pending_pick is None
    engine.telemetry.close()
