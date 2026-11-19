"""이야깃거리 데이터셋(RAG)과 방송 간 재사용 막기 (#332, CORNER_DESIGN 5.9절)."""
import asyncio
import json
import math
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

from onair_engine import bank as bank_cli
from onair_engine.domain import Prompt, StationConfig
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.inputs import talk_bank
from onair_engine.inputs.talk_bank import (
    COMMON,
    BankItem,
    TalkBank,
    load_items,
    persona_text,
    shared_bank,
)
from onair_engine.inputs.topic_plan import Angle, TopicPlan, build_prompt, parse_angles
from onair_engine.inputs.topic_usage import TopicUsage
from onair_engine.main import load_config
from onair_engine.pipeline import embed
from onair_engine.pipeline.embed import DummyEmbedder, EmbedError, GeminiEmbedder, dot, unit
from onair_engine.pipeline.llm import LlmError
from onair_engine.telemetry import Telemetry
from test_scheduler import PROFILE, RULES, Capture

ENGINE_ROOT = Path(__file__).resolve().parents[1]

# 낱말이 들어 있으면 그 축이 선다 — 뜻이 가까운 글끼리 가깝게 나오는 흉내
AXES = ["간식", "시험", "출근", "심야", "아침", "노래"]


class AxisEmbedder:
    name = "axis-test"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts, *, query=False):
        if self.fail:
            raise EmbedError("429")
        self.calls.append(list(texts))
        return [unit([float(axis in text) for axis in AXES] + [0.05]) for text in texts]


def _item(n: int, theme: str, title: str, *, fmt="talk", persona=COMMON) -> BankItem:
    return BankItem(f"{fmt}.{persona}.{n}", theme, title, ("꺼내기", "풀기", "마무리"), fmt, persona)


ITEMS = [
    _item(1, "겨울 간식", "붕어빵"),
    _item(2, "겨울 간식", "밤샘 간식", persona="night"),
    _item(3, "겨울 간식", "출근길 간식", persona="morning"),
    _item(4, "시험 기간", "벼락치기"),
    _item(5, "겨울 간식", "간식과 노래", fmt="music"),
]
PERSONAS = {"night": "심야 라디오. 차분하다", "morning": "아침 출근 라디오. 밝다"}


def _bank(tmp_path=None, embedder=None, **kw) -> TalkBank:
    return TalkBank(ITEMS, PERSONAS, embedder or AxisEmbedder(),
                    cache_path=tmp_path / "vectors.sqlite" if tmp_path else None, **kw)


# ── 임베딩 어댑터 ──────────────────────────────────────────────────────

def test_dummy_embedder_is_stable_and_ranks_overlapping_text_closer():
    vectors = asyncio.run(DummyEmbedder().embed(["겨울 간식 붕어빵", "겨울 간식 호빵", "출근길 지하철"]))
    again = asyncio.run(DummyEmbedder().embed(["겨울 간식 붕어빵"]))
    assert vectors[0] == again[0], "같은 글은 언제나 같은 벡터다"
    assert all(math.isclose(dot(v, v), 1.0, abs_tol=1e-5) for v in vectors)
    assert dot(vectors[0], vectors[1]) > dot(vectors[0], vectors[2])


def test_gemini_embedder_batches_and_normalizes(monkeypatch):
    bodies = []

    def fake_post(url, body, headers, timeout, provider):
        assert url.endswith("/models/gemini-embedding-001:batchEmbedContents")
        assert headers == {"x-goog-api-key": "k"}
        bodies.append(body)
        return {"embeddings": [{"values": [3.0, 4.0]} for _ in body["requests"]]}

    monkeypatch.setattr(embed, "_post_json", fake_post)
    client = GeminiEmbedder(api_key="k", dim=2)
    vectors = asyncio.run(client.embed([f"글 {n}" for n in range(150)], query=True))
    assert [len(b["requests"]) for b in bodies] == [100, 50], "한 번에 100건까지만 받는다"
    request = bodies[0]["requests"][0]
    assert request["taskType"] == "RETRIEVAL_QUERY" and request["outputDimensionality"] == 2
    assert request["content"] == {"parts": [{"text": "글 0"}]}
    assert len(vectors) == 150 and [round(x, 3) for x in vectors[0]] == [0.6, 0.8], "길이를 1로 맞춘다"
    asyncio.run(client.embed(["이야깃거리"]))
    assert bodies[-1]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"
    assert client.name == "gemini-embedding-001-2", "모델과 차원이 캐시의 열쇠다"


def test_gemini_embedder_failures(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiEmbedder()

    def broken(*args):
        raise LlmError("gemini 임베딩 429: quota")

    monkeypatch.setattr(embed, "_post_json", broken)
    with pytest.raises(EmbedError, match="429"):
        asyncio.run(GeminiEmbedder(api_key="k").embed(["글"]))
    monkeypatch.setattr(embed, "_post_json", lambda *args: {"embeddings": []})
    with pytest.raises(EmbedError, match="0건만"):
        asyncio.run(GeminiEmbedder(api_key="k").embed(["글"]))


# ── 검색 ──────────────────────────────────────────────────────────────

def test_search_takes_the_nearest_preset_and_common_items_of_the_format():
    found = asyncio.run(_bank().search("겨울 간식 이야기", fmt="talk", persona="심야 스터디 라디오. 조용하다", k=5))
    assert found.persona == "night", "호스트의 페르소나와 가장 닮은 프리셋"
    assert [h.item.title for h in found.hits] == ["밤샘 간식", "붕어빵"], "닮은 프리셋 것이 공통보다 앞, 다른 프리셋·형식·주제는 뺀다"
    assert all(h.score > 0.9 for h in found.hits) and found.skipped == 0


def test_search_format_falls_back_to_talk_and_respects_k_and_exclusions():
    bank = _bank()
    music = asyncio.run(bank.search("간식", fmt="music", persona="", k=5))
    assert [h.item.title for h in music.hits] == ["간식과 노래"] and music.persona is None
    news = asyncio.run(bank.search("간식", fmt="news", persona="", k=5))
    assert [h.item.title for h in news.hits] == ["붕어빵"], "이야깃거리가 없는 형식은 토크 것을 쓴다"
    night = asyncio.run(bank.search("간식", fmt="talk", persona="심야", k=1, exclude={"talk.night.2"}))
    assert [h.item.title for h in night.hits] == ["붕어빵"] and night.skipped == 1, "다른 방송이 쓴 것은 뺀다"
    assert asyncio.run(bank.search("여행", fmt="talk", persona="", k=5)).hits == (), "가까운 것이 없으면 찾지 않는다"


def test_index_embeds_only_what_the_cache_lacks(tmp_path):
    first = AxisEmbedder()
    assert asyncio.run(_bank(tmp_path, first).index()) == len(ITEMS) + len(PERSONAS)
    second = AxisEmbedder()
    bank = _bank(tmp_path, second)
    assert asyncio.run(bank.index()) == 0 and second.calls == [], "캐시에 있으면 다시 임베딩하지 않는다"
    assert asyncio.run(bank.search("간식", fmt="talk", persona="", k=5)).hits
    changed = AxisEmbedder()
    changed.name = "other-model"
    assert asyncio.run(_bank(tmp_path, changed).index()) == len(ITEMS) + len(PERSONAS), "모델을 바꾸면 다시 만든다"


def test_shipped_dataset_loads_and_covers_every_cell():
    directory = ENGINE_ROOT / "config" / "talk_bank"
    seed = yaml.safe_load((directory / "themes.yaml").read_text(encoding="utf-8"))
    items = load_items(directory)
    assert len({item.id for item in items}) == len(items), "id가 겹치지 않는다"
    cells = {(item.format, item.persona) for item in items}
    assert cells == {(fmt, pid) for fmt in seed["formats"] for pid in [COMMON, *seed["personas"]]}
    assert set(talk_bank.load_personas(directory)) == set(seed["personas"])
    assert {item.theme for item in items} == set(seed["themes"]), "빈 주제가 없다"
    assert all(len(item.points) >= bank_cli.MIN_POINTS for item in items)
    assert {item.kind for item in items} == set(seed["kinds"]), "방식을 나눠 만든 것만 싣는다"
    # 방송 하나가 보는 묶음(공통 + 프리셋 하나)에 주제마다 갈래가 한 칸 몫은 있고 방식이 섞여 있다.
    # 가득 찬 묶음은 한 칸 몫의 두 배다 — 닮아서 버린 자리는 비워 두므로 다 차지 않은 묶음이 있다
    for fmt, spec in seed["formats"].items():
        for pid in seed["personas"]:
            pools: dict[str, list] = {theme: [] for theme in seed["themes"]}
            for item in items:
                if item.format == fmt and item.persona in (COMMON, pid):
                    pools[item.theme].append(item)
            thin = [theme for theme, pool in pools.items()
                    if len(pool) < spec["per_theme"] or len({item.kind for item in pool}) < 4]
            assert not thin, f"{fmt}.{pid}: 갈래가 모자라거나 방식이 쏠린 주제 {thin[:5]}"
        for theme in seed["themes"]:
            titles = [item.title for item in items if item.format == fmt and item.theme == theme]
            assert len(set(titles)) == len(titles), f"{fmt} {theme}: 같은 제목이 둘"


# ── 방송별 사용 기록 ───────────────────────────────────────────────────

def _usage(tmp_path, **kw) -> TopicUsage:
    return TopicUsage(tmp_path / "usage.sqlite", AxisEmbedder(), **kw)


def test_usage_remembers_what_other_broadcasts_used(tmp_path):
    usage = _usage(tmp_path)
    usage.record("st_a", "겨울 간식", Angle("붕어빵", ("팥",), "talk.common.1"))
    usage.record("st_a", "겨울 간식", Angle("새로 짠 갈래", ("호빵",)))
    assert usage.used_items("st_b") == {"talk.common.1"}
    assert usage.used_items("st_a") == set(), "자기 방송의 기록은 막지 않는다"
    usage._db.execute("UPDATE used_angles SET at = ?", (time.time() - 31 * 86400,))
    assert usage.used_items("st_b") == set(), "기간이 지나면 다시 써도 된다"


def test_screen_drops_angles_another_broadcast_already_told(tmp_path):
    usage = _usage(tmp_path, threshold=0.9)
    told = Angle("시험 전날", ("시험 범위",))
    vectors = asyncio.run(usage.screen("st_a", [told])).vectors
    usage.record("st_a", "시험", told, vectors[told.title])
    usage.record("st_a", "간식", Angle("붕어빵", ("팥",), "talk.common.1"))
    angles = [Angle("시험 기간의 밤", ("시험 걱정",)), Angle("출근길", ("출근 버스",)),
              Angle("붕어빵 다시", ("아침",), "talk.common.1")]
    screened = asyncio.run(usage.screen("st_b", angles))
    assert [a.title for a in screened.kept] == ["출근길"]
    assert {a.title for a in screened.reused} == {"시험 기간의 밤", "붕어빵 다시"}, "비슷한 갈래와 이미 쓴 이야깃거리"
    assert set(screened.vectors) == {a.title for a in angles}
    assert asyncio.run(usage.screen("st_a", angles)).reused == [], "자기 방송의 기록과는 견주지 않는다"
    floor = asyncio.run(usage.screen("st_b", angles, keep_min=2))
    assert [a.title for a in floor.kept] == ["시험 기간의 밤", "출근길"], "다 빼지 않는다 — 덜 겹치는 것부터 되살린다"
    assert asyncio.run(usage.nearby_titles("st_b", "시험 이야기")) == ["시험 전날"]


def test_screen_keeps_everything_when_embedding_fails(tmp_path):
    usage = TopicUsage(tmp_path / "usage.sqlite", AxisEmbedder(fail=True))
    angles = [Angle("시험", ("범위",))]
    assert asyncio.run(usage.screen("st_b", angles)).kept == angles
    assert asyncio.run(usage.nearby_titles("st_b", "시험")) == []


# ── 갈래 계획 연결 ─────────────────────────────────────────────────────

class FixedLlm:
    """reply가 목록이면 부를 때마다 차례로 하나씩 돌려준다."""

    def __init__(self, reply):
        self.replies = list(reply) if isinstance(reply, list) else None
        self.reply = reply
        self.prompts: list[Prompt] = []

    async def complete(self, prompt, *, max_tokens):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else self.reply
        if isinstance(reply, Exception):
            raise reply
        return reply


def _reply(*angles: dict) -> str:
    return json.dumps({"angles": list(angles)}, ensure_ascii=False)


def _plan(tmp_path, reply, station="st_a", bank=None, usage=None, topic="겨울 간식", **kw):
    telemetry = Telemetry(tmp_path / "m.sqlite", station)
    llm = FixedLlm(reply)
    plan = TopicPlan(llm, topic, count=3, violates=lambda text: None, telemetry=telemetry, bank=bank,
                     usage=usage, station_id=station, fmt="talk", persona="심야 스터디 라디오", **kw)
    return plan, llm, telemetry


def test_prompt_lists_references_by_number_and_parse_maps_them_back():
    prompt = build_prompt("겨울 간식", 3, references=ITEMS[:2], avoid=["호빵 데우기"]).user
    assert '<data label="참고 이야깃거리">\n1. 붕어빵\n- 꺼내기\n- 풀기\n- 마무리\n2. 밤샘 간식' in prompt
    assert '"source": 번호 또는 null' in prompt
    assert '<data label="다른 방송에서 다룬 갈래">\n- 호빵 데우기' in prompt
    assert "source" not in build_prompt("겨울 간식", 3).user, "참고 자료가 없으면 예전 프롬프트 그대로다"
    text = _reply({"title": "가", "points": ["a"], "source": 2}, {"title": "나", "points": ["b"], "source": "1"},
                  {"title": "다", "points": ["c"], "source": None}, {"title": "라", "points": ["d"], "source": 9},
                  {"title": "마", "points": ["e"], "source": True})
    assert [a.source for a in parse_angles(text, ["x", "y"])] == ["y", "x", None, None, None]
    assert [a.source for a in parse_angles(text)] == [None] * 5


def test_plan_feeds_references_to_the_llm_and_keeps_their_source(tmp_path):
    reply = _reply({"title": "밤샘 간식 고르기", "points": ["편의점"], "source": 1},
                   {"title": "새로 짠 갈래", "points": ["호빵"], "source": None})
    plan, llm, telemetry = _plan(tmp_path, reply, bank=_bank())
    asyncio.run(plan.run())
    assert "1. 밤샘 간식" in llm.prompts[0].user and "2. 붕어빵" in llm.prompts[0].user
    assert [(a.title, a.source) for a in (plan.next_angle(), plan.next_angle())] == [
        ("밤샘 간식 고르기", "talk.night.2"), ("새로 짠 갈래", None)]
    telemetry.close()
    db = sqlite3.connect(tmp_path / "m.sqlite")
    topic, persona, hits = db.execute("SELECT topic, persona, hits_json FROM talk_bank_log").fetchone()
    assert (topic, persona) == ("겨울 간식", "night") and [h["id"] for h in json.loads(hits)] == [
        "talk.night.2", "talk.common.1"]
    assert json.loads(db.execute("SELECT angles_json FROM topic_plan_log").fetchone()[0])[0]["source"] == "talk.night.2"
    events = [s for (s,) in db.execute("SELECT summary FROM event_log WHERE kind = 'topic_plan'")]
    assert events[:2] == ["이야깃거리 2건 찾음", "이야기 갈래 2개 준비 (데이터셋에서 1개)"]


def test_plan_uses_references_as_they_are_when_the_llm_fails(tmp_path):
    plan, _, telemetry = _plan(tmp_path, LlmError("429"), bank=_bank())
    asyncio.run(plan.run())
    assert [(a.title, a.source) for a in (plan.next_angle(), plan.next_angle())] == [
        ("밤샘 간식", "talk.night.2"), ("붕어빵", "talk.common.1")]
    assert plan.next_angle() is None
    telemetry.close()
    db = sqlite3.connect(tmp_path / "m.sqlite")
    assert db.execute("SELECT method FROM topic_plan_log").fetchone() == ("bank",)


def test_plan_goes_on_without_references_when_search_fails(tmp_path):
    reply = _reply({"title": "새로 짠 갈래", "points": ["호빵"]})
    plan, llm, telemetry = _plan(tmp_path, reply, bank=_bank(embedder=AxisEmbedder(fail=True)))
    asyncio.run(plan.run())
    assert "참고 이야깃거리" not in llm.prompts[0].user and plan.next_angle().title == "새로 짠 갈래"
    telemetry.close()
    db = sqlite3.connect(tmp_path / "m.sqlite")
    assert "429" in db.execute("SELECT error FROM talk_bank_log").fetchone()[0]


def test_next_broadcast_does_not_reuse_what_the_first_one_told(tmp_path):
    usage = _usage(tmp_path, threshold=0.9)
    first, _, t1 = _plan(tmp_path, _reply({"title": "밤샘 간식 고르기", "points": ["편의점 간식"], "source": 1},
                                          {"title": "시험 전날", "points": ["시험 범위"]},
                                          {"title": "꺼내지 않은 갈래", "points": ["아침 노래"]}),
                         bank=_bank(), usage=usage)
    asyncio.run(first.run())
    assert first.next_angle().source == "talk.night.2" and first.next_angle().title == "시험 전날"
    t1.close()
    rows = usage._db.execute("SELECT broadcast, topic, title, item_id, vec IS NOT NULL FROM used_angles").fetchall()
    assert rows == [("st_a", "겨울 간식", "밤샘 간식 고르기", "talk.night.2", 1),
                    ("st_a", "겨울 간식", "시험 전날", None, 1)], "꺼낸 갈래만 적는다"

    replies = [_reply({"title": "시험 기간의 밤", "points": ["시험 걱정"]},
                      {"title": "출근길 풍경", "points": ["출근 버스"]},
                      {"title": "아침 노래", "points": ["아침에 듣는 노래"]}),
               # 빠진 만큼 다시 짠 것 — 이미 짠 갈래와 또 겹치는 갈래는 버리고 새 갈래만 남는다
               _reply({"title": "출근길 풍경", "points": ["출근 버스"]}, {"title": "시험 날", "points": ["시험 종"]},
                      {"title": "출근길 노래", "points": ["출근하며 듣는 노래"]})]
    second, llm, t2 = _plan(tmp_path, replies, station="st_b", bank=_bank(), usage=usage)
    asyncio.run(second.run())
    prompt = llm.prompts[0].user
    assert "1. 붕어빵" in prompt and "2. " not in prompt, "첫 방송이 쓴 이야깃거리는 참고 자료에서 빠진다"
    assert '<data label="다른 방송에서 다룬 갈래">\n- 밤샘 간식 고르기' in prompt
    refill = llm.prompts[1].user
    assert "갈래 1개" in refill and "참고 이야깃거리" not in refill, "빠진 수만큼만 다시 짠다"
    assert '<data label="다른 방송에서 다룬 갈래">\n- 밤샘 간식 고르기\n- 시험 기간의 밤' in refill
    assert '<data label="다룬 갈래">\n- 출근길 풍경\n- 아침 노래' in refill
    titles = [second.next_angle().title for _ in range(2)]
    assert titles == ["출근길 풍경", "아침 노래"], "겹치는 갈래는 뺀다"
    assert second.next_angle() is None, "다시 짠 갈래도 다른 방송과 겹치면 뺀다 — need만큼만 본다"
    t2.close()
    db = sqlite3.connect(tmp_path / "m.sqlite")
    events = [s for (s,) in db.execute("SELECT summary FROM event_log WHERE station_id = 'st_b'")]
    assert events[:2] == ["이야깃거리 1건 찾음 (다른 방송이 써서 1건 제외)",
                          "이야기 갈래 2개 준비 (다른 방송과 겹쳐 2개 제외)"]


def test_refill_replaces_angles_dropped_for_reuse(tmp_path):
    usage = _usage(tmp_path, threshold=0.9)
    told = Angle("시험 전날", ("시험 범위",))
    usage.record("st_a", "시험", told, asyncio.run(usage.screen("st_a", [told])).vectors[told.title])
    replies = [_reply({"title": "시험 기간의 밤", "points": ["시험 걱정"]}, {"title": "출근길 풍경", "points": ["출근 버스"]},
                      {"title": "아침 노래", "points": ["아침에 듣는 노래"]}),
               _reply({"title": "심야 간식", "points": ["심야에 먹는 간식"]})]
    plan, llm, telemetry = _plan(tmp_path, replies, station="st_b", usage=usage)
    asyncio.run(plan.run())
    assert len(llm.prompts) == 2
    assert [plan.next_angle().title for _ in range(3)] == ["출근길 풍경", "아침 노래", "심야 간식"]
    telemetry.close()
    assert usage._db.execute("SELECT COUNT(*) FROM used_angles WHERE broadcast = 'st_b' AND vec IS NOT NULL"
                             ).fetchone() == (3,), "다시 짠 갈래도 벡터와 함께 적는다"

    usage = _usage(tmp_path / "other", threshold=0.9)
    usage.record("st_a", "시험", told, asyncio.run(usage.screen("st_a", [told])).vectors[told.title])
    failing, llm, telemetry = _plan(tmp_path, [replies[0], LlmError("429")], station="st_c", usage=usage)
    asyncio.run(failing.run())
    assert [a.title for a in failing._angles] == ["출근길 풍경", "아침 노래"], "다시 짜기가 실패하면 남은 것으로 간다"
    telemetry.close()


# ── 데이터셋 만들기 ────────────────────────────────────────────────────

def test_build_prompt_differs_for_common_and_persona_cells():
    common = bank_cli.build_prompt({"겨울 간식": ["", ""], "시험 기간": ["", ""]}, "토크 방송이다.", None).user
    assert "갈래 2개" in common and "- 토크 방송이다." in common and "어느 방송에서나" in common
    assert '<data label="주제 목록">\n- 겨울 간식\n- 시험 기간' in common and "스테이션 컨셉" not in common
    assert "이미 있는 갈래" not in common and '"kind"' not in common
    persona = {"concept": "심야 스터디 라디오", "tone": "차분한 존댓말", "description": "조용한 DJ",
               "forbidden": ["다그치기"]}
    own = bank_cli.build_prompt({"겨울 간식": [""]}, "음악 방송이다.", persona).user
    assert '<data label="스테이션 컨셉">\n심야 스터디 라디오\n조용한 DJ\n차분한 존댓말' in own
    assert "다음은 넣지 않는다: 다그치기" in own and "어느 방송에서나" not in own


def test_build_prompt_asks_for_kinds_and_shows_what_exists():
    kinds = {"장면": "한 장면을 따라간다.", "갈림": "갈리는 선택을 다룬다.", "요령": "요령을 모은다."}
    text = bank_cli.build_prompt({"겨울 간식": ["장면", "갈림"], "시험 기간": ["갈림"]}, "토크 방송이다.", None, kinds=kinds,
                                 existing={"겨울 간식": ["붕어빵", "호떡"], "시험 기간": [], "남의 주제": ["버림"]}).user
    assert "방식마다 하나씩" in text and '<data label="주제 목록">\n- 겨울 간식: 장면, 갈림\n- 시험 기간: 갈림' in text
    assert "  - 장면: 한 장면을 따라간다." in text and "  - 갈림: 갈리는 선택을 다룬다." in text
    assert "요령을 모은다" not in text, "달라고 하지 않은 방식은 풀이하지 않는다"
    assert '<data label="이미 있는 갈래">\n- 겨울 간식: 붕어빵 / 호떡\n</data>' in text and '"kind": "방식 이름"' in text


def test_kinds_are_spread_so_one_broadcast_sees_them_all():
    kinds = ["장면", "갈림", "요령", "달라짐", "어긋남", "사람"]
    for theme_no in range(len(kinds) + 1):
        common = bank_cli.kinds_for(kinds, 4, theme_no, 0)
        assert len(set(common)) == 4
        for persona_no in range(1, 6):
            own = bank_cli.kinds_for(kinds, 4, theme_no, persona_no)
            assert len(set(own)) == 4 and set(common) | set(own) == set(kinds), "공통 + 프리셋 하나에 여섯 방식이 다 있다"
    assert bank_cli.kinds_for(kinds, 4, 0, 0) != bank_cli.kinds_for(kinds, 4, 1, 0), "주제마다 시작을 돌린다"
    assert bank_cli.kinds_for([], 3, 0, 2) == ["", "", ""]
    assert bank_cli.kinds_for(["장면", "갈림"], 3, 0, 1) == ["장면", "갈림", "장면"], "방식이 모자라면 돌려 쓴다"


def test_parse_items_keeps_usable_angles_only():
    text = "결과:\n" + json.dumps({"themes": [
        {"theme": "겨울 간식", "angles": [
            {"kind": " 갈림 ", "title": "붕어빵", "points": ["a", "b", "c"]},
            "갈래 아님",
            {"title": "말할 거리가 모자란 갈래", "points": ["a", "b"]},
            {"title": "연락처", "points": ["a", "b", "example 닷컴"]}]},
        {"theme": "이름을 바꿔 적은 주제", "angles": [{"title": "벼락치기", "points": ["a", "b", "c", "d"]}]},
        {"theme": "남는 주제", "angles": [{"title": "버림", "points": ["a", "b", "c"]}]},
        {"theme": "겨울 간식", "angles": "목록 아님"},
    ]}, ensure_ascii=False)
    items = bank_cli.parse_items(text, ["겨울 간식", "시험 기간"], "talk", "night",
                                 lambda t: "domain" if "닷컴" in t else None)
    assert [(i.theme, i.title, i.format, i.persona) for i in items] == [
        ("겨울 간식", "붕어빵", "talk", "night"), ("시험 기간", "벼락치기", "talk", "night")]
    assert items[0].id == bank_cli.item_id("talk", "night", "겨울 간식", "붕어빵") and items[0].id.startswith("talk.night.")
    assert [i.kind for i in items] == ["갈림", ""]
    with pytest.raises(ValueError):
        bank_cli.parse_items("JSON 아님", ["겨울 간식"], "talk", COMMON, lambda t: None)


def test_build_writes_one_file_per_cell_and_search_reads_it_back(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "bank"
    directory.mkdir()
    (directory / "themes.yaml").write_text(yaml.safe_dump({
        "personas": ["saebyeok"], "themes": ["겨울 간식", "시험 기간", "출근길"],
        "formats": {"talk": {"per_theme": 1, "guide": "토크 방송이다."}}}, allow_unicode=True), encoding="utf-8")

    class ThemeLlm:
        async def complete(self, prompt, *, max_tokens):
            themes = [line[2:] for line in prompt.user.split('<data label="주제 목록">\n')[1].split("\n</data>")[0].splitlines()]
            who = "밤 " if "스테이션 컨셉" in prompt.user else ""
            return json.dumps({"themes": [{"theme": t, "angles": [{"title": f"{who}{t} 이야기", "points": ["a", "b", "c"]}]}
                                          for t in themes]}, ensure_ascii=False)

    monkeypatch.setattr(bank_cli, "make_llm", lambda *a, **kw: ThemeLlm())
    common = ["--dir", str(directory), "--cache", str(tmp_path / "vectors.sqlite")]
    bank_cli.cli([*common, "build", "--themes-per-call", "2", "--personas-dir", str(ENGINE_ROOT / "config" / "personas"),
                  "--safety-rules", str(RULES)])
    assert sorted(p.name for p in (directory / "items").iterdir()) == ["talk.common.jsonl", "talk.saebyeok.jsonl"]
    items = load_items(directory)
    assert len(items) == 6 and {i.persona for i in items} == {COMMON, "saebyeok"}
    assert talk_bank.load_personas(directory)["saebyeok"].startswith("심야 스터디 라디오. ")
    capsys.readouterr()
    bank_cli.cli([*common, "index"])
    assert "이야깃거리 6건, 벡터 7건" in capsys.readouterr().out, "이야깃거리 여섯 + 프리셋 하나"
    bank_cli.cli([*common, "search", "겨울 간식 이야기", "--min-score", "0.3",
                  "--persona", str(ENGINE_ROOT / "config" / "personas" / "saebyeok.yaml")])
    out = capsys.readouterr().out
    assert "닮은 프리셋: saebyeok" in out and "겨울 간식 — 겨울 간식 이야기" in out


class KindLlm:
    """달라고 한 자리마다 갈래를 하나씩 낸다. 제목은 titles(주제, 방식, 몇 번째 물음)가 정한다."""

    def __init__(self, titles):
        self.titles = titles
        self.prompts: list[str] = []

    async def complete(self, prompt, *, max_tokens):
        self.prompts.append(prompt.user)
        asked = sum("스테이션 컨셉" in p for p in self.prompts) if "스테이션 컨셉" in prompt.user else 0
        lines = prompt.user.split('<data label="주제 목록">\n')[1].split("\n</data>")[0].splitlines()
        groups = []
        for line in lines:
            theme, wanted = line[2:].split(": ")
            groups.append({"theme": theme, "angles": [
                {"kind": kind, "title": self.titles(theme, kind, asked), "points": ["a", "b", "c"]}
                for kind in wanted.split(", ")]})
        return json.dumps({"themes": groups}, ensure_ascii=False)


def _kind_seed(directory, themes):
    directory.mkdir(exist_ok=True)
    (directory / "themes.yaml").write_text(yaml.safe_dump({
        "personas": ["saebyeok"], "themes": themes, "kinds": {"장면": "장면이다.", "갈림": "갈림이다.", "요령": "요령이다."},
        "formats": {"talk": {"per_theme": 2, "guide": "토크 방송이다."}}}, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def _build(directory, tmp_path, *extra):
    bank_cli.cli(["--dir", str(directory), "--cache", str(tmp_path / "vectors.sqlite"), "build",
                  "--personas-dir", str(ENGINE_ROOT / "config" / "personas"), "--safety-rules", str(RULES), *extra])


def test_build_drops_a_look_alike_and_asks_for_that_slot_again(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "bank"
    _kind_seed(directory, ["겨울 간식"])

    def titles(theme, kind, asked):
        # 공통: 장면 = 간식 이야기, 갈림 = 시험 이야기. 프리셋의 요령은 처음에 공통의 장면과 같은 축(간식)이고,
        # 다시 물으면 다른 축(출근)을 낸다
        if asked == 0:
            return {"장면": "간식 굽는 골목", "갈림": "시험 전날의 선택"}[kind]
        return {"요령": "간식 고르는 요령" if asked == 1 else "출근 전에 챙기는 요령", "장면": "노래 듣는 심야"}[kind]

    llm = KindLlm(titles)
    monkeypatch.setattr(bank_cli, "make_llm", lambda *a, **kw: llm)
    monkeypatch.setattr(bank_cli, "make_embedder", lambda kind: AxisEmbedder())
    _build(directory, tmp_path, "--embedder", "dummy")
    items = load_items(directory)
    assert {(i.persona, i.kind, i.title) for i in items} == {
        (COMMON, "장면", "간식 굽는 골목"), (COMMON, "갈림", "시험 전날의 선택"),
        ("saebyeok", "장면", "노래 듣는 심야"), ("saebyeok", "요령", "출근 전에 챙기는 요령")}
    assert "겹쳐 버린 것 1건" in capsys.readouterr().out
    again = llm.prompts[-1]
    assert "- 겨울 간식: 요령\n" in again, "버린 자리만 다시 묻는다"
    assert "- 겨울 간식: 간식 굽는 골목 / 시험 전날의 선택 / 노래 듣는 심야" in again, "이미 있는 갈래를 보여 준다"
    cached = sqlite3.connect(tmp_path / "vectors.sqlite").execute("SELECT count(*) FROM vectors").fetchone()[0]
    assert cached >= len(items), "만들면서 임베딩한 것을 벡터 캐시에 둔다"


def test_build_fill_keeps_what_exists_and_makes_only_what_is_missing(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "bank"
    _kind_seed(directory, ["겨울 간식"])
    llm = KindLlm(lambda theme, kind, asked: f"{theme} {kind} {'프리셋' if asked else '공통'}")
    monkeypatch.setattr(bank_cli, "make_llm", lambda *a, **kw: llm)
    _build(directory, tmp_path)
    first = {i.id for i in load_items(directory)}
    assert len(first) == 4 and "제목이 같은 것만 거른다" in capsys.readouterr().out
    # 주제를 하나 더하고, 있던 주제의 한 칸에서 하나를 지운다
    _kind_seed(directory, ["겨울 간식", "시험 기간"])
    path = directory / "items" / "talk.common.jsonl"
    path.write_text(path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    calls = len(llm.prompts)
    _build(directory, tmp_path, "--fill")
    items = load_items(directory)
    assert first <= {i.id for i in items} and len(items) == 8
    assert all("겨울 간식: 장면, 갈림" not in p for p in llm.prompts[calls:]), "있는 것은 다시 만들지 않는다"
    # --fill이 없으면 처음부터 다시 만든다. 씨앗에서 뺀 주제의 것은 남기지 않는다
    _kind_seed(directory, ["시험 기간"])
    _build(directory, tmp_path, "--only", "talk.saebyeok")
    assert {(i.theme, i.persona) for i in load_items(directory)} == {("시험 기간", COMMON), ("시험 기간", "saebyeok")}


# ── 엔진 연결 ──────────────────────────────────────────────────────────

def _engine(tmp_path, **settings) -> StationEngine:
    cfg = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30, topic="겨울 간식")
    return StationEngine(cfg, EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
                                             safety_rules_path=RULES, **settings), Capture())


def test_engine_wires_the_bank_and_usage_into_the_plan(tmp_path):
    talk_bank._SHARED.clear()
    off = _engine(tmp_path)
    assert off.topic_plan._bank is None and off.topic_plan._usage is None, "설정이 없으면 예전처럼 LLM만으로 짠다"
    settings = {"talk_bank_dir": ENGINE_ROOT / "config" / "talk_bank", "talk_bank_cache": tmp_path / "vectors.sqlite",
                "topic_usage_path": tmp_path / "usage.sqlite", "topic_reuse_days": 7}
    engine = _engine(tmp_path, **settings)
    plan = engine.topic_plan
    assert plan._bank is not None and plan._usage.days == 7 and plan._station_id == "st_test"
    again = _engine(tmp_path, **settings).topic_plan
    assert plan._broadcast.startswith("bc_") and again._broadcast != plan._broadcast, (
        "스테이션 id는 고정 채널이라 방송이 바뀌어도 같다 — 방송마다 새 id로 가린다")
    assert plan._persona == persona_text(PROFILE.concept, PROFILE.tone) and plan._fmt == "talk"
    assert _engine(tmp_path, **settings).topic_plan._bank is plan._bank, "데이터셋은 엔진 프로세스에 하나다"
    assert shared_bank(settings["talk_bank_dir"], DummyEmbedder(), None) is plan._bank
    talk_bank._SHARED.clear()


def test_engine_broadcasts_without_the_bank_when_it_cannot_open(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    engine = _engine(tmp_path, talk_bank_dir=ENGINE_ROOT / "config" / "talk_bank", embedder="gemini",
                     topic_usage_path=tmp_path / "usage.sqlite")
    assert engine.topic_plan is not None and engine.topic_plan._bank is None and engine.topic_plan._usage is None
    assert "TALK_BANK_OFF" in caplog.text and "GEMINI_API_KEY" in caplog.text


def test_dummy_broadcast_plans_with_the_shipped_dataset(tmp_path):
    # 더미 임베딩(글자 겹침)으로도 주제와 같은 낱말이 든 이야깃거리가 걸린다 — 키 없이 흐름을 돌려 본다
    talk_bank._SHARED.clear()
    engine = _engine(tmp_path, talk_bank_dir=ENGINE_ROOT / "config" / "talk_bank",
                     talk_bank_cache=tmp_path / "vectors.sqlite", topic_usage_path=tmp_path / "usage.sqlite")
    engine.topic_plan._bank.min_score = 0.2
    engine.topic_plan._llm = FixedLlm(LlmError("꺼짐"))
    asyncio.run(engine.topic_plan.run())
    angle = engine.topic_plan.next_angle()
    assert angle is not None and angle.source and angle.source.startswith("talk.")
    assert engine.topic_plan._usage.used_items("bc_next") == {angle.source}
    row = engine.topic_plan._usage._db.execute("SELECT broadcast, station_id FROM used_angles").fetchone()
    assert row == (engine.topic_plan._broadcast, "st_test")
    # 같은 채널(같은 스테이션 id)의 다음 방송 — 앞 방송이 쓴 이야깃거리를 참고 자료로 받지 않는다
    following = _engine(tmp_path, talk_bank_dir=ENGINE_ROOT / "config" / "talk_bank",
                        talk_bank_cache=tmp_path / "vectors.sqlite", topic_usage_path=tmp_path / "usage.sqlite")
    following.topic_plan._bank.min_score = 0.2
    following.topic_plan._llm = FixedLlm(LlmError("꺼짐"))
    asyncio.run(following.topic_plan.run())
    assert angle.source not in {a.source for a in following.topic_plan._angles}
    talk_bank._SHARED.clear()


def test_load_config_reads_bank_settings(tmp_path):
    raw = yaml.safe_load((ENGINE_ROOT / "config" / "station.example.yaml").read_text(encoding="utf-8"))
    raw["pipeline"].update(talk_bank="config/talk_bank", embedder="gemini", topic_usage="var/u.sqlite", topic_reuse_days=3)
    path = tmp_path / "station.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    settings = load_config(path)[1]
    assert (settings.talk_bank_dir, settings.embedder) == (Path("config/talk_bank"), "gemini")
    assert (settings.topic_usage_path, settings.topic_reuse_days) == (Path("var/u.sqlite"), 3.0)
    for key in ("talk_bank", "embedder", "topic_usage", "topic_reuse_days"):
        raw["pipeline"].pop(key)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    settings = load_config(path)[1]
    assert settings.talk_bank_dir is None and settings.topic_usage_path is None and settings.embedder == "dummy"
