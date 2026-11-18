"""심사 AI를 믿어도 되는가 — 방송 기록에 결함을 일부러 넣고 잡아내는지 본다 (#320).

    python -m onair_engine.eval.judge_defects var/scenario/<실행>/aud_3/run-1 --out var/judge-defects --yes

기록 하나에 결함을 하나씩만 넣은 사본을 만들고, 그 결함이 속한 기준만 채점한다. 결함을 넣은 자리에서 그 항목이
"문제 있음"으로 나오면 잡은 것이다. 심사 AI가 어느 항목을 못 잡는지 알아야 그 항목의 점수를 믿지 않을 수 있다 —
못 잡은 항목은 지시문을 고치거나 기준표에서 reference: true(참고용)로 돌린다.

넣는 글은 손으로 쓴 고정 문장이다. 방송 주제와 무관하게 어느 기록에나 넣을 수 있게 썼다.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..main import load_env
from .judge import RUBRIC_FILE, Judge, Rubric, cost_usd, load_rubric, make_judge_llm
from .judge_rules import KST, load_broadcast

_TALK_SKIP = ("opening", "closing", "music_intro")
_NO_SONG_WORDS = ("노래", "곡", "음악", "가수", "밴드", "제목")


@dataclass
class Context:
    played: list[dict]
    run: dict
    report: dict
    persona: dict
    topic: str
    hour: int
    marks: list[str]  # 결함을 넣은 멘트의 대본 — 채점 뒤 이 멘트를 가리켰는지 본다
    chats: list[str]  # 결함으로 넣은 청취자 글의 request_id


@dataclass
class Defect:
    key: str
    name: str
    items: tuple[str, ...]  # 이 가운데 하나가 잡으면 된다. 첫째가 겨냥한 항목이다
    apply: Callable[[Context], bool]  # 넣지 못하는 기록이면 False


# ── 기록 고치기 ────────────────────────────────────────────────────────

def _speech(ctx: Context) -> list[int]:
    return [i for i, e in enumerate(ctx.played) if e["kind"] == "speech" and e.get("script")]


def _mid_talk(ctx: Context, frac: float) -> int | None:
    """이야기 코너 한가운데의 멘트 — 바로 뒤에도 같은 코너의 멘트가 이어지는 자리."""
    idx = _speech(ctx)
    good = [i for n, i in enumerate(idx[:-1])
            if ctx.played[i]["corner_type"] not in _TALK_SKIP
            and ctx.played[idx[n + 1]]["corner_type"] == ctx.played[i]["corner_type"]]
    if not good:
        return None
    return min(good, key=lambda i: abs(i - frac * len(ctx.played)))


def _insert(ctx: Context, after: int, script: str, *, reflected: list[str] | None = None, sec: float | None = None) -> None:
    prev = ctx.played[after]
    script = script.strip()
    ctx.played.insert(after + 1, {
        "at": round(prev["at"] + prev["sec"], 2), "kind": "speech", "sec": sec or round(len(script) / 7, 2),
        "segment_id": f"defect_{len(ctx.marks)}", "corner_type": prev["corner_type"], "corner": prev.get("corner"),
        "script": script, "text": None, "reflected": reflected or []})
    ctx.marks.append(script)


def _insert_chat(ctx: Context, before: int, body: str, who: str = "defect") -> str:
    rid = f"defect_c{len(ctx.chats) + 1}"
    at = round(ctx.played[before]["at"] - 0.5, 2)
    ctx.played.insert(before, {"at": at, "kind": "input", "sec": 0, "segment_id": None, "corner_type": None,
                               "corner": None, "script": None, "text": f"채팅 {rid} ({who}): {body}", "reflected": []})
    ctx.chats.append(rid)
    return rid


def _say(script: str | Callable[[Context], str], frac: float = 0.5, **kwargs) -> Callable[[Context], bool]:
    def apply(ctx: Context) -> bool:
        at = _mid_talk(ctx, frac)
        if at is None:
            return False
        _insert(ctx, at, script(ctx) if callable(script) else script, **kwargs)
        return True

    return apply


def _bodies(ctx: Context) -> dict[str, str]:
    out = {}
    for e in ctx.played:
        if e["kind"] == "input" and ": " in (e.get("text") or ""):
            head, body = e["text"].split(": ", 1)
            out[head.split()[1]] = body
    return out


def _taking(ctx: Context, want: Callable[[str], bool]) -> tuple[int, str, str] | None:
    """청취자 글을 받은 멘트 가운데 조건에 맞는 첫째 → (멘트 자리, request_id, 글)."""
    bodies = _bodies(ctx)
    for i in _speech(ctx):
        entry = ctx.played[i]
        if entry["corner_type"] in ("opening", "closing"):
            continue
        for rid in entry.get("reflected") or []:
            if rid in bodies and want(bodies[rid]):
                return i, rid, bodies[rid]
    return None


def _wrong_target(ctx: Context) -> bool:
    hit = _taking(ctx, lambda body: not any(w in body for w in _NO_SONG_WORDS))
    if hit is None:
        return False
    entry = ctx.played[hit[0]]
    entry["script"] = "방금 나간 곡을 듣고 보내 주신 반응이 도착했어요. 곡이 마음에 드셨나 봐요. " + entry["script"]
    ctx.marks.append(entry["script"])
    return True


def _unanswered(ctx: Context) -> bool:
    """받지 않은 질문 — 받은 질문의 답을 지우는 방법은 쓰지 않는다. 답이 다음 멘트까지 이어진 기록에서는 답이 남는다."""
    at = _mid_talk(ctx, 0.25)
    if at is None or ctx.played[-1]["at"] - ctx.played[at]["at"] < 240:
        return False
    _insert_chat(ctx, at, "DJ님 질문 있어요! 방송 끝나고 나면 보통 뭐 하면서 쉬세요? 꼭 알려 주세요")
    return True


def _fabricated(ctx: Context) -> bool:
    hit = _taking(ctx, lambda body: True)
    if hit is None:
        return False
    entry = ctx.played[hit[0]]
    entry["script"] += " 그분은 내일 면접을 보러 간다고도 하셨고, 지난주에 이사를 했다고도 적어 주셨어요."
    ctx.marks.append(entry["script"])
    return True


def _chat_and_reply(body: str, reply: str) -> Callable[[Context], bool]:
    def apply(ctx: Context) -> bool:
        at = _mid_talk(ctx, 0.5)
        if at is None:
            return False
        rid = _insert_chat(ctx, at, body)
        _insert(ctx, at + 1, reply, reflected=[rid])
        return True

    return apply


def _ignored_listener(ctx: Context) -> bool:
    idx = [i for i in _speech(ctx) if ctx.played[i]["corner_type"] not in ("opening", "closing")]
    if len(idx) < 12:
        return False
    lines = [f"{ctx.topic} 얘기 너무 좋아요 저도 할 말 많은데", "저는 어릴 때 할머니 댁에서 먹던 게 제일 기억나요",
             "DJ님 제 얘기도 한 번만 읽어 주세요", "아까부터 계속 보내고 있는데 안 보이시나요", "저 진짜 열심히 듣고 있어요 ㅠㅠ"]
    for n, body in enumerate(lines):
        spot = idx[(n + 1) * len(idx) // 8] + n  # 앞에 넣은 만큼 자리가 밀린다
        _insert_chat(ctx, spot, body, who="defect_ignored")
    return True


def _after_goodbye(ctx: Context) -> bool:
    closing = [i for i in _speech(ctx) if ctx.played[i]["corner_type"] == "closing"]
    if not closing:
        return False
    _insert(ctx, closing[-1], "자, 그럼 이제 새로운 이야기를 시작해 볼까요. 오늘 준비한 두 번째 주제는 여름휴가 계획이에요. "
            "다들 올여름에 어디로 떠나고 싶으세요?")
    return True


def _wrong_song(ctx: Context) -> bool:
    for i, entry in enumerate(ctx.played):
        if entry["kind"] != "music" or " - " not in (entry.get("text") or ""):
            continue
        before = next((j for j in range(i - 1, -1, -1) if ctx.played[j]["kind"] != "input"), None)
        intro = ctx.played[before] if before is not None else None
        title = entry["text"].rsplit(" - ", 1)[0]
        if intro and intro["kind"] == "speech" and intro.get("script") and title in intro["script"]:
            intro["script"] = intro["script"].replace(title, "한여름 밤의 소나기")
            ctx.marks.append(intro["script"])
            return True
    return False


def _after_song(ctx: Context) -> bool:
    for i, entry in enumerate(ctx.played[:-1]):
        nxt = next((e for e in ctx.played[i + 1:] if e["kind"] != "input"), None)
        if entry["kind"] == "music" and nxt and nxt["kind"] == "speech" and nxt.get("script") \
                and nxt["corner_type"] != "closing":
            nxt["script"] = ("방금 들으신 곡은 여름 해변의 축제였습니다. 신나는 댄스곡 잘 들으셨나요. "
                             "자, 그럼 오늘 방송을 처음부터 다시 소개해 드릴게요.")
            ctx.marks.append(nxt["script"])
            return True
    return False


def _same_content(ctx: Context) -> bool:
    lines = ["물 한 잔 마시고 갈게요. 목이 마르면 집중이 잘 안 되니까요.", "목마르지 않게 물 한 잔 드세요. 그래야 집중이 돼요.",
             "집중하려면 물이 필요해요. 한 잔 마시고 올까요.", "물 한 잔, 잊지 마세요. 목이 마르면 집중이 흐려져요.",
             "물은 드셨나요. 목이 마르면 집중하기 어려우니 한 잔 드세요."]
    for n, line in enumerate(lines):
        at = _mid_talk(ctx, 0.15 + 0.17 * n)
        if at is None:
            return False
        _insert(ctx, at, line)
    return True


def _dropped_promise(ctx: Context) -> bool:
    at = _mid_talk(ctx, 0.5)
    if at is None:
        return False
    _insert(ctx, at, "네, 그건 그렇고 내일은 전국이 대체로 맑겠다고 하네요.")
    _insert(ctx, at, "자, 지금부터 제가 오래 아껴 둔 이야기를 하나 들려드릴게요. 잘 들어 보세요. 그게 뭐냐면요.")
    return True


def _same_transition(ctx: Context) -> bool:
    for n in range(5):
        at = _mid_talk(ctx, 0.12 + 0.18 * n)
        if at is None:
            return False
        _insert(ctx, at, "자, 그럼 다음 이야기로 한번 넘어가 볼까요.")
    return True


def _contradiction(ctx: Context) -> bool:
    early, late = _mid_talk(ctx, 0.25), _mid_talk(ctx, 0.75)
    if early is None or late is None or early == late:
        return False
    _insert(ctx, late, "저는 단 음식을 제일 좋아해서, 하루도 빠짐없이 챙겨 먹고 있어요.")
    _insert(ctx, early, "저는 단 음식을 전혀 못 먹어요. 태어나서 한 번도 입에 대 본 적이 없어요.")
    return True


def _callback(ctx: Context) -> bool:
    hit = _taking(ctx, lambda body: len(body) >= 6)
    late, later = _mid_talk(ctx, 0.7), _mid_talk(ctx, 0.9)
    if hit is None or late is None or later is None or late <= hit[0]:
        return False
    body = hit[2][:40]
    _insert(ctx, later, f"{body}. 이 말, 다시 떠오르네요.", reflected=[hit[1]])
    _insert(ctx, late, f"{body}라고 하신 분이 있었죠. {body}라고요.", reflected=[hit[1]])
    return True


def _other_persona(ctx: Context) -> bool:
    own = ctx.persona.get("persona_id")
    other = "beongae" if own != "beongae" else "saebyeok"
    ctx.run["persona"] = f"config/personas/{other}.yaml"
    ctx.run.get("scenario", {}).get("station", {}).pop("persona", None)
    return True


def _forbidden(ctx: Context) -> bool:
    if not any("다그치" in rule for rule in ctx.persona.get("forbidden") or []):
        return False
    return _say("지금 쉬면 안 됩니다. 당장 책을 펴고 더 하세요. 그 정도 공부로는 한참 부족합니다.")(ctx)


def _wrong_time(ctx: Context) -> bool:
    night = ctx.hour >= 20 or ctx.hour < 6
    return _say("좋은 아침입니다. 출근길 햇살이 눈부시죠. 오늘 점심 메뉴는 정하셨나요." if night
                else "자정이 훌쩍 넘은 깊은 새벽이네요. 다들 잠자리에 드실 시간이죠.")(ctx)


def _long_ment(ctx: Context) -> bool:
    return _say("오늘은 조금 길게 이야기해 볼게요. " + "천천히 숨을 고르고, 어깨에 힘을 빼 보세요. " * 9, sec=48.0)(ctx)


DEFECTS: list[Defect] = [
    Defect("written_style", "글말투 멘트", ("A1",), _say(
        lambda c: f"금일 방송에서는 {c.topic}에 관하여 논의를 진행하고자 하는 바입니다. 이에 대한 청취자 여러분의 고견을 "
                  "수렴하도록 하겠습니다.")),
    Defect("long_breath", "한 호흡에 못 읽는 문장", ("A2",), _say(
        "어제 저녁에 집에 돌아오는 길에 골목 어귀에서 우연히 마주친 오래된 가게의 간판을 한참 바라보다가 문득 어린 시절에 "
        "할머니 손을 잡고 걷던 시장 골목이 떠올라서 그 자리에 한동안 서 있었던 기억이 오늘 다시 떠오르는 밤입니다.")),
    Defect("cliche", "빈말만 있는 상투구", ("A3", "D1"), _say(
        "여러분의 소중한 하루 끝에 작은 위로와 따뜻한 온기가 마음 한켠에 스며드는, 그런 의미 있는 시간이 되었으면 좋겠습니다. "
        "우리 모두의 마음속에 작은 쉼표 하나가 되기를 바랍니다.")),
    Defect("wrong_target", "이야기에 대한 글을 곡에 대한 반응으로 소개", ("B1", "B3"), _wrong_target),
    Defect("unanswered", "DJ에게 한 질문을 끝까지 받지 않음", ("B2", "B6"), _unanswered),
    Defect("fabricated", "청취자가 하지 않은 말을 덧붙임", ("B3",), _fabricated),
    Defect("rude_reply", "불만에 비꼬는 대응", ("B4", "E3"), _chat_and_reply(
        "말이 너무 많아요 노래 좀 틀어요", "말이 많다고 하신 분, 듣기 싫으면 안 들으시면 됩니다. 저는 제 방식대로 할게요.")),
    Defect("joke_serious", "농담을 진지한 사연으로 받음", ("B5",), _chat_and_reply(
        "저 방금 붕어빵 천 개 먹고 배가 풍선 돼서 하늘로 날아가는 중ㅋㅋㅋㅋ",
        "붕어빵을 천 개나 드시고 몸이 하늘로 떠오르셨다니 정말 큰일입니다. 지금 바로 병원에 가셔서 치료를 받으셔야 해요. "
        "빠른 쾌유를 진심으로 빕니다.")),
    Defect("ignored_listener", "한 청취자의 글 다섯 건을 한 번도 받지 않음", ("B6",), _ignored_listener),
    Defect("after_goodbye", "끝인사 뒤에 새 이야기를 시작", ("C1", "I1", "C3", "F2"), _after_goodbye),
    Defect("same_transition", "같은 전환 문구를 다섯 번", ("C3", "D3"), _same_transition),
    Defect("wrong_song", "소개한 곡과 나간 곡이 다름", ("C2", "G3"), _wrong_song),
    Defect("off_topic", "이음말 없이 주제와 무관한 이야기", ("F2", "C3", "I1"), _say(
        "그런데 요즘 주택 담보 대출 금리가 많이 올랐죠. 대출 상품은 여러 은행을 비교해 보고 고르셔야 합니다. "
        "고정 금리와 변동 금리의 차이도 꼭 확인하세요.")),
    Defect("empty", "알맹이 없는 멘트", ("D1", "D2", "A3"), _say(
        "그런 느낌이 있죠. 뭐랄까, 그런 순간들이요. 그런 마음이 드는 밤이에요. 그런 거죠.")),
    Defect("unclear", "무슨 말인지 알 수 없는 멘트", ("D2", "D1"), _say(
        "그래서 그게 그렇게 되면 그쪽도 그런 거라서, 아까 그것처럼 그러니까 그건 그대로 두고 그때 그 얘기로 넘어가면 그게 맞죠.")),
    Defect("same_content", "같은 내용을 말만 바꿔 다섯 번", ("D3",), _same_content),
    Defect("false_fact", "틀린 사실과 지어낸 출처", ("D4",), _say(
        "참고로 붕어빵은 천구백구십 년에 프랑스 파리에서 처음 만들어졌고, 세계보건기구는 하루에 세 개씩 먹으라고 권장하고 있어요.")),
    Defect("casual_tone", "설정과 다른 반말", ("E1", "G2", "E3"), _say(
        "야 다들 뭐 하냐. 나 지금 완전 신났어. 같이 달려 보자.")),
    Defect("other_persona", "다른 DJ의 설정으로 바꿔 치기", ("E2",), _other_persona),
    Defect("forbidden", "금지 사항(다그치기)을 어김", ("E3",), _forbidden),
    Defect("wrong_time", "방송 시각과 어긋난 인사", ("F1",), _wrong_time),
    Defect("grammar", "틀린 조사와 맞춤법", ("G1",), _say(
        "저는 어제 붕어빵를 먹었는데 맛이 됬어요. 몇일 뒤에 또 먹을께요. 왠만하면 같이 먹는 게 낳아요.")),
    Defect("honorific", "사물 높임", ("G2",), _say(
        "방금 채팅이 도착하셨어요. 신청하신 곡이 곧 나오실 예정이세요. 따뜻한 차도 준비되셨습니다.")),
    Defect("symbols", "읽을 수 없는 기호", ("H1",), _say("오늘 기분 최고예요 😊 **진짜** 좋아요 (웃음) http://example.com")),
    Defect("notation", "읽는 법이 모호한 표기", ("H2",), _say(
        "오늘은 D-4니까 24/7 응원할게요. 간식은 3~4개, 5kg 이하로 드시고 물은 500ml씩 vs 커피는 1/2잔만 드세요.")),
    Defect("long_ment", "48초짜리 멘트", ("H3",), _long_ment),
    Defect("after_song", "곡 뒤에 다른 곡을 들었다고 말하고 방송을 다시 소개", ("I2", "C2", "I1"), _after_song),
    Defect("dropped_promise", "들려주겠다고 한 이야기를 버리고 딴 얘기", ("I1", "D2"), _dropped_promise),
    Defect("contradiction", "앞뒤가 어긋나는 자기 이야기", ("I3",), _contradiction),
    Defect("callback", "지난 글을 맥락 없이 두 번 더 꺼냄", ("I4", "B7"), _callback),
    Defect("restart", "방송 중간에 새로 시작하는 인사", ("I5",), _say(
        lambda c: f"안녕하세요, {c.persona.get('concept')}의 {c.persona.get('dj_name')}입니다. 지금 시작합니다.")),
]


# ── 사본 만들기와 판정 ──────────────────────────────────────────────────

def make_variant(base: Path, defect: Defect, out: Path) -> dict | None:
    """결함 하나를 넣은 사본을 out에 만든다. 이 기록에는 넣을 수 없는 결함이면 None."""
    b = load_broadcast(base)
    start = b.local_start() or datetime.now(KST)
    ctx = Context(played=copy.deepcopy(json.loads((base / "played.json").read_text(encoding="utf-8"))["played"]),
                  run=json.loads((base / "run.json").read_text(encoding="utf-8")),
                  report=json.loads((base / "report.json").read_text(encoding="utf-8")),
                  persona=b.persona, topic=b.topic, hour=start.hour, marks=[], chats=[])
    if not defect.apply(ctx):
        return None
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    ctx.run.setdefault("scenario", {})["name"] = defect.key
    (out / "played.json").write_text(json.dumps({"played": ctx.played}, ensure_ascii=False), encoding="utf-8")
    (out / "run.json").write_text(json.dumps(ctx.run, ensure_ascii=False), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(ctx.report, ensure_ascii=False), encoding="utf-8")
    info = {"key": defect.key, "name": defect.name, "items": list(defect.items), "marks": ctx.marks, "chats": ctx.chats}
    (out / "defect.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


def caught(info: dict, result: dict, base: dict | None, path: Path) -> list[str]:
    """결함을 잡은 항목들. 넣은 멘트나 글을 가리킨 문제가 있거나, 가리킬 자리가 없는 항목이면 원본보다 문제가 늘었을 때다."""
    b = load_broadcast(path)
    marked = [m for m in b.ments if m.script in info["marks"]]
    spots = {m.id for m in marked} | {c.id for c in b.chats if c.request_id in info["chats"]}
    spots |= {cid for m in marked for cid in m.takes}  # 고친 멘트가 받은 글을 가리켜도 그 결함을 짚은 것이다
    spots |= {c.who for c in b.chats if c.request_id in info["chats"]}
    hits = []
    for key in info["items"]:
        slot = result["items"][key]
        before = len(base["items"][key]["findings"]) if base else 0
        if any(f["at"] in spots for f in slot["findings"]) or (
                (not spots or slot["unit"] == "broadcast") and len(slot["findings"]) > before):
            hits.append(key)
    return hits


def render(rows: list[dict], base: Path, cost: str) -> str:
    done = [r for r in rows if r["status"] != "넣지 못함"]
    lines = ["# 결함 주입 — 심사 AI가 일부러 넣은 결함을 잡는가", "", f"- 원본 기록: `{base}`", f"- {cost}",
             (f"- 넣은 결함 {len(done)}개 가운데 겨냥한 항목이 잡은 것 {sum(r['status'] == '잡음' for r in done)}개, "
              f"다른 항목이 잡은 것 {sum(r['status'] == '다른 항목이 잡음' for r in done)}개, "
              f"못 잡은 것 {sum(r['status'] == '못 잡음' for r in done)}개"), "",
             "| 결함 | 겨냥한 항목 | 결과 | 잡은 항목 | 심사 AI가 적은 까닭 |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['name']} | {r['items'][0]} | {r['status']} | {', '.join(r['hits']) or '-'} | {r['why'][:140]} |")
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> int:
    rubric: Rubric = load_rubric(args.rubric)
    criterion_of = {item.key: c.key for c, item in rubric.pairs()}
    wanted = set(args.only.split(",")) if args.only else None
    variants = []
    for defect in DEFECTS:
        if wanted and defect.key not in wanted:
            continue
        info = make_variant(args.base, defect, args.out / defect.key)
        variants.append((defect, info))
    todo = [(d, i) for d, i in variants if i]
    print(f"결함 {len(todo)}개 (넣지 못한 것 {len(variants) - len(todo)}개), 심사 모델 {args.model or rubric.judge.model}")
    if not args.yes and input("진행할까요? [y/N] ").strip().lower() != "y":
        return 1
    llm = make_judge_llm(rubric.judge, args.model)
    judge = Judge(rubric, llm, parallel=args.parallel)
    base_file = args.base / "judge.json"
    base = json.loads(base_file.read_text(encoding="utf-8")) if base_file.exists() else None

    async def one(defect: Defect, info: dict) -> dict:
        path = args.out / defect.key
        result = await judge.judge(load_broadcast(path), {criterion_of[k] for k in defect.items})
        (path / "judge.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        hits = caught(info, result, base, path)
        status = "잡음" if defect.items[0] in hits else "다른 항목이 잡음" if hits else "못 잡음"
        why = next((f["why"] for k in hits for f in result["items"][k]["findings"]), "")
        print(f"{status} · {defect.name} ({', '.join(hits) or '-'})")
        return {"key": defect.key, "name": defect.name, "items": list(defect.items), "status": status,
                "hits": hits, "why": why}

    rows = await asyncio.gather(*(one(d, i) for d, i in todo))
    rows += [{"key": d.key, "name": d.name, "items": list(d.items), "status": "넣지 못함", "hits": [], "why": ""}
             for d, i in variants if not i]
    usage = getattr(llm, "usage", {})
    cost = (f"심사 AI 호출 {judge.calls}번(실패 {judge.failures}) · 입력 {usage.get('in', 0):,} · 출력 {usage.get('out', 0):,} 토큰 · "
            f"어림 비용 ${cost_usd(rubric.judge, usage):.2f}")
    (args.out / "defects.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out / "defects.md").write_text(render(rows, args.base, cost), encoding="utf-8")
    print(f"{cost}\n결과: {args.out / 'defects.md'}")
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onair-judge-defects",
                                     description="방송 기록에 결함을 넣고 심사 AI가 잡는지 본다")
    parser.add_argument("base", type=Path, help="결함을 넣을 실행 폴더 (played.json이 있는 곳)")
    parser.add_argument("--out", type=Path, required=True, help="결함을 넣은 사본과 결과를 쓸 폴더")
    parser.add_argument("--rubric", type=Path, default=RUBRIC_FILE)
    parser.add_argument("--model", default=None)
    parser.add_argument("--only", default=None, metavar="KEYS", help="넣을 결함 (예: grammar,wrong_time)")
    parser.add_argument("--parallel", type=int, default=6)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(cli())
