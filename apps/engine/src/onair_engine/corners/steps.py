"""단계별 프롬프트 빌더 — 코너는 설정(config/formats)이 정하고, 단계 유형마다 지시문은 여기서 만든다.

외부 텍스트(곡 정보, 기사, 사연, 청취자 채팅, 오늘의 주제)는 전부 데이터 블록으로 넣는다 (prompt_data.py).
지시문마다 marker 구절이 하나씩 들어간다 — 더미 LLM이 프롬프트만 보고 단계를 알아보는 표식이다
(pipeline/llm.py detect_step). 지시문을 고칠 때 marker를 지우지 않는다 (test_steps가 검사한다).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..domain import Material, Prompt, StationProfile
from ..prompt_data import (
    ANGLE,
    CALLBACK,
    CHAT,
    CORNERS,
    COVERED_ANGLES,
    DILEMMA,
    NOW,
    SOURCES,
    STORY,
    TOPIC,
    TRACK,
    TREND,
    VOTE_RESULT,
    data_block,
)
from .base import system_prompt

# 채팅 인용 지시 — 인용할지 말지를 LLM에 맡기지 않는다. 넣었으면 반드시 인용한다 (CORNER_DESIGN 5.1절)
# 질문은 답만 하고 누가 물었는지 빼먹기 쉽다 — 2026-11-12 평가에서 DJ에게 묻는 채팅 36건 중 12건이 그랬다 (T-007)
# 예시에 문장 끝을 넣지 않는다 — 넣으면 그 말끝을 그대로 따라 쓴다. 2026-11-12 평가에서 '~분이 있네요'는
# 반말 DJ의 채팅 멘트 60건 모두에, '~분이 있다'는 존댓말 DJ의 60건 중 20건에 그대로 나왔다 (T-007)
QUOTE_RULE = ("아래 청취자 채팅을 짧게 인용하고 한 문장으로 반응한다. 인용은 '~라고 하신 분'처럼 청취자의 말임을 밝힌다. "
              "채팅이 질문이면 '~냐고 물어보신 분'처럼 청취자가 물었다는 것부터 말하고 짧게 답한다. "
              "문장 끝은 DJ의 말투로 맺는다. 닉네임이나 아이디는 말하지 않는다.")
# 다음 단계부터 이 채팅이 화제가 된다 — 청취자가 흐름이 바뀐 이유를 알 수 있게 한다 (CORNER_DESIGN 5.2절)
STEER_RULE = "그리고 이 얘기를 조금 더 해 보겠다고 자연스럽게 말한다."
# 한 멘트에 입력 여러 건 (계약 v2 input_refs, #267)
# 묶어서 "여러 분이 ~라고 하셨네요"라고 하면 누구의 말도 인용되지 않는다 — 한 건씩 밝혀야 자기 말인 줄 안다
MULTI_QUOTE_RULE = ("채팅이 여러 건이다. 한 건씩 따로 인용하고 각각 한 문장으로 반응한다. "
                    "한 건도 빠뜨리지 않고, 여러 건을 한 문장으로 묶지 않는다.")
MULTI_STEER_RULE = "그리고 마지막에 인용한 채팅의 얘기를 조금 더 해 보겠다고 자연스럽게 말한다."
# 주 소재(사연·신청곡)가 있는 멘트 — 채팅 때문에 사연이 요약되거나 곡 소개가 밀리면 안 된다
WITH_FEATURE_RULE = ("이 멘트의 중심은 위에서 지시한 내용이다. 채팅 인용은 그 앞이나 뒤에 짧게 붙이고, "
                     "중심 내용을 줄이지 않는다. 위의 문장 수 제한에 채팅 인용은 넣지 않는다.")
# 곡 소개 뒤에는 곡이 바로 나간다. 채팅이 한 건일 때는 인용 → 곡 소개 순서로 쓰는데(평가 90건 중 83건),
# 두 건이 되면 곡을 먼저 소개하고 채팅을 읽는다(36건 중 35건, 2026-11-12 평가) — 순서를 정해 준다
TRACK_LAST_RULE = "채팅 인용을 모두 마친 뒤에 곡을 소개하고, 곡 소개로 멘트를 끝낸다. 이 멘트 바로 뒤에 곡이 나간다."
# 몰린 입력은 개인이 아니라 집계로 인정한다 — 특정 채팅을 인용하거나 건수를 지어내지 않는다 (9.2절)
AGGREGATE_RULE = ("멘트 첫머리에 지금 채팅창에서 아래 화제 얘기가 많이 오간다고 한 문장으로 언급한다. "
                  "특정 채팅을 인용하거나 숫자를 말하지 않는다.")
PREVIEW_RULE = ("멘트 끝에 사연 문자가 많이 와 있으니 이따 '{title}' 시간에 읽겠다고 "
                "한 문장으로 알린다. 숫자를 말하지 않는다.")
# 다시 꺼내기 — 이미 방송에 나간 청취자 글이다. 닉네임은 블록에 있는 것만 쓴다(채팅은 넣지 않는다)
CALLBACK_RULE = ("아래 앞서 방송에서 소개한 청취자 이야기를 '아까 ~라고 해 주셨죠'처럼 짧게 다시 꺼내 지금 화제와 잇는다. "
                 "내용을 지어내거나 뜻을 바꾸지 않고, 닉네임은 블록에 있을 때만 부른다.")
# 시간 맥락은 화제를 꺼내는 단계에만 붙인다 — 매 멘트에 붙이면 "벌써 자정이네요"가 되풀이된다
TIME_RULE = "아래 지금 시각은 이야기와 어울릴 때만 짧게 쓰고, 꼭 말하지 않아도 된다."
# 답이 없는 방송에서 계속 묻지 않는다 — 조용한 방송의 이야기 멘트 75%가 질문으로 끝났다 (2026-11-11 기준선, #262)
NO_QUESTION_RULE = "청취자에게 질문하지 않는다. 묻는 대신 DJ가 생각을 풀어 가고, 질문으로 끝내지 않는다."
# 곡 뒤에 고정 문구("다시 …로 돌아왔어요")를 틀지 않는다 — 그 자리는 이 멘트가 잇는다
AFTER_SONG_RULE = "곡이 끝나고 다시 말을 잇는 자리다. 새로 인사하거나 돌아왔다고 말하지 않는다."
BACK_ANNOUNCE_RULE = "먼저 아래 방금 나간 곡을 한마디로 받고, 거기서 이야기로 넘어간다. 곡을 다시 소개하지는 않는다."
ANGLE_CONTINUE_RULE = "아래 이번 갈래는 앞에서 하던 이야기다. 처음 꺼내듯 말하지 않고 하던 이야기를 이어 간다."
ANGLE_HANDOFF_RULE = ("아래 다룬 갈래 가운데 마지막 것이 바로 앞에서 한 이야기다. "
                      "그 이야기에서 자연스럽게 넘어오며 이번 갈래를 꺼낸다.")
# 이야기 덩어리 — 곡 사이 말을 한 번에 쓴다. 문단 하나가 세그먼트 하나로 합성된다 (pipeline._write_block)
BLOCK_STEP = "talk_block"
BLOCK_MARKER = "이야기 한 덩어리"
PREV_TRACK = "방금 나간 곡"


def track_line(track: dict) -> str:
    line = f"곡: {track['title']} — {track.get('artist') or '알 수 없는 아티스트'}"
    return line + (f" (무드: {track['mood']})" if track.get("mood") else "")


def news_lines(items: list[dict]) -> str:
    # URL은 소리 내어 읽을 수 없고 L2 룰(URL 금지)에 걸린다 — 매체 이름만 넘긴다
    return "\n".join(f"- {it['title']}: {it['summary']} (출처: {it['source']})" for it in items)


def _with_topic(text: str, m: Material) -> str:
    return text + ("\n" + data_block(TOPIC, m.topic) if m.topic else "")


def _opening_greeting(m: Material) -> str:
    # 첫 곡은 백엔드가 방송 시작과 함께 이미 틀었다 — track(music_ref)이 아니라 extra로 받는다
    if first := m.extra.get("first_song"):
        return ("방송을 시작한다. 인사하고, 방금 흘러나온 첫 곡을 한마디로 언급하는 오프닝 멘트를 "
                "3문장 이내로 작성하라.\n" + data_block(TRACK, track_line(first)))
    return "방송을 시작한다. 스테이션 컨셉에 맞는 오프닝 멘트를 3문장 이내로 작성하라."


def _topic_intro(m: Material) -> str:
    return _with_topic("오늘의 주제를 소개하는 멘트를 2문장 이내로 작성하라. 주제는 아래 데이터 블록에 있다.", m)


def _corner_preview(m: Material) -> str:
    guide = m.extra.get("guide")
    text = "오늘 이어질 코너를 예고하는 멘트를 3문장 이내로 작성하라. "
    text += (f"참여 방법도 안내하되 이 내용을 빠뜨리지 않는다: {guide}" if guide
             else "청취자 참여 방법은 말하지 않는다.")
    titles = "\n".join(f"- {t}" for t in m.extra.get("corners", []))
    return text + ("\n" + data_block(CORNERS, titles) if titles else "")


def _angle(m: Material) -> str:
    """이번 갈래(inputs/topic_plan.py) 블록. 갈래 계획이 없거나 채팅이 화제를 바꿨으면 비어 있다."""
    angle = m.extra.get("angle")
    return "\n" + data_block(ANGLE, angle.text()) if angle else ""


def _question(m: Material) -> str:
    if m.extra.get("question"):
        return " 끝에 청취자에게 가볍게 질문을 하나 던진다."
    return f" {NO_QUESTION_RULE}" if m.extra.get("no_question") else ""


def _after_song(m: Material) -> tuple[str, str]:
    """곡 뒤에 말을 잇는 자리의 지시와 데이터 블록. 곡 뒤가 아니면 둘 다 빈 문자열."""
    track = m.extra.get("after_song")
    if not track:
        return "", ""
    if m.extra.get("back_announce"):
        return f" {AFTER_SONG_RULE} {BACK_ANNOUNCE_RULE}", "\n" + data_block(PREV_TRACK, track_line(track))
    return f" {AFTER_SONG_RULE}", ""


def _angle_rule(m: Material, fresh: str) -> str:
    """이번 갈래를 어떻게 꺼낼지 — 하던 갈래면 잇고, 새 갈래면 앞 갈래에서 넘어온다."""
    if not m.extra.get("angle"):
        return ""
    if m.extra.get("angle_state") == "continue":
        return f" {ANGLE_CONTINUE_RULE}"
    if m.extra.get("covered_angles"):
        return f" {ANGLE_HANDOFF_RULE}"
    return fresh


def _opener_context(m: Material) -> str:
    """화제를 꺼내는 단계의 덧붙임 — 질문, 이미 다룬 갈래, 지금 시각."""
    text = _question(m)
    if covered := m.extra.get("covered_angles"):
        text += (" 아래 이미 다룬 갈래는 되풀이하지 않는다.\n"
                 + data_block(COVERED_ANGLES, "\n".join(f"- {t}" for t in covered)))
    if now := m.extra.get("now"):
        text += f"\n{TIME_RULE}\n" + data_block(NOW, now)
    return text


def _talk_open(m: Material) -> str:
    text = "오늘의 주제로 이야기를 꺼내는 멘트를 3문장 이내로 작성하라."
    # 오프닝(topic_intro)이 주제를 이미 소개했다 — 2026-11-07 실행에서 바로 뒤에 한 번 더 소개했다
    text += _angle_rule(m, " 주제는 오프닝에서 이미 소개했으니 다시 소개하지 않고, 아래 이번 갈래로 바로 시작한다.")
    rule, block = _after_song(m)
    return _with_topic(text + rule + _opener_context(m), m) + _angle(m) + block


def _talk_resume(m: Material) -> str:
    # 다른 말 코너를 거쳐 왔으면 직전에 접착 문구(resume)가 "다시 … 돌아왔어요"를 이미 말했다
    text = ("다른 순서를 지나 다시 오늘의 이야기로 돌아온 멘트를 3문장 이내로 작성하라. "
            "주제를 처음 소개하듯 말하지 않고, 돌아왔다는 말도 하지 않는다.")
    text += _angle_rule(m, " 아래 이번 갈래로 화제를 옮긴다.") or " 앞에서 한 말과 다른 면을 꺼낸다."
    rule, block = _after_song(m)
    return _with_topic(text + rule + _opener_context(m), m) + _angle(m) + block


def _talk(m: Material) -> str:
    # 소재 하나로 한 멘트 — 편성 관리자가 채팅·다시 꺼낼 이야기·갈래의 말할 거리 중 하나를 골라 넣는다 (7.7절)
    text = "지금 화제로 이야기를 이어가는 멘트를 3문장 이내로 작성하라. 직전 멘트와 같은 말을 되풀이하지 않는다."
    text += f" {NO_QUESTION_RULE}" if m.extra.get("no_question") else " 질문으로 끝내지 않아도 된다."
    if callback := m.extra.get("callback"):
        return _with_topic(f"{text} {CALLBACK_RULE}", m) + "\n" + data_block(CALLBACK, callback.text())
    if m.extra.get("angle"):
        text += " 아래 이번 갈래의 말할 거리로 이야기한다."
    return _with_topic(text, m) + _angle(m)


def _talk_block(m: Material) -> str:
    """곡과 곡 사이 말 전체를 한 번에 — 채팅이 없는 동안의 DJ 주도 진행 (CORNER_DESIGN 7.7.1절)."""
    parts = int(m.extra.get("parts", 3))
    text = (f"곡과 곡 사이에 DJ가 혼자 이어 말할 {BLOCK_MARKER}를 쓴다. 문단 {parts}개로 나누고, 문단마다 2~3문장을 쓰고, "
            "문단 사이는 빈 줄로 띄운다. 한 사람이 한 호흡으로 말하듯 문단이 앞 문단을 받아 이어져야 한다. "
            "문단마다 새로 인사하거나 주제를 다시 소개하지 않고, 같은 말을 되풀이하지 않는다.")
    rule, blocks = _after_song(m)
    text += rule
    if song := m.extra.get("song_story"):
        # 주제 이야기만 되풀이하지 않게 가끔 방금 나간 곡 이야기로 쉬어 간다. 다음 곡 이야기는 할 수 없다 —
        # 이 덩어리를 쓰는 시점에는 백엔드가 다음 곡을 아직 알려 주지 않는다 (ENGINE_TUNING_LOG T-008)
        text += (" 이번에는 주제 이야기를 잠시 쉬고, 아래 방금 나간 곡을 듣고 떠오른 이야기를 한다 — "
                 "곡의 분위기, 그 곡이 어울리는 시간이나 장면 같은 것이다. "
                 "곡을 다시 소개하거나 다시 틀겠다고 하지 않는다. "
                 "수상·발매 시기·일화처럼 확인할 수 없는 사실은 말하지 않는다. "
                 "오늘 주제와 닿는 데가 있으면 가볍게 잇는다.")
        blocks += "\n" + data_block(PREV_TRACK, track_line(song))
    elif m.extra.get("angle"):
        text += _angle_rule(m, " 주제는 이미 소개했으니 다시 소개하지 않고, 아래 이번 갈래로 바로 시작한다.")
        text += " 이번 갈래의 말할 거리를 문단에 차례로 풀어 쓴다."
    else:
        text += " 오늘 주제에서 아직 하지 않은 말을 풀어 간다."
    if callback := m.extra.get("callback"):
        text += f" 문단 하나에서는 {CALLBACK_RULE}"
        blocks += "\n" + data_block(CALLBACK, callback.text())
    if m.extra.get("question"):
        text += " 마지막 문단 끝에만 청취자에게 가볍게 질문을 하나 던진다. 다른 문단에서는 묻지 않는다."
    else:
        text += f" {NO_QUESTION_RULE}"
    if covered := m.extra.get("covered_angles"):
        text += " 아래 이미 다룬 갈래는 되풀이하지 않는다."
        blocks += "\n" + data_block(COVERED_ANGLES, "\n".join(f"- {t}" for t in covered))
    if now := m.extra.get("now"):
        text += f" {TIME_RULE}"
        blocks += "\n" + data_block(NOW, now)
    return _with_topic(text, m) + ("" if m.extra.get("song_story") else _angle(m)) + blocks


_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.?!…])\s+")


def split_block(raw: str, parts: int) -> list[str]:
    """덩어리 대본을 문단으로 — 문단 하나가 세그먼트 하나다. 거부면 [REJECT], 쓸 문단이 없으면 [].

    LLM이 빈 줄 없이 한 문단으로 쓰면 문장 수로 고르게 나눈다. 계획보다 많이 나온 문단은 마지막 문단에 붙인다.
    """
    # 순환 import 회피 — llm이 이 모듈을 쓴다
    from ..pipeline.llm import REJECT, LlmError, clean_script

    def clean(text: str) -> str | None:
        try:
            return clean_script(text)
        except LlmError:  # 기호뿐인 문단
            return None

    whole = clean(raw)
    if whole is None:
        return []
    if whole == REJECT:
        return [REJECT]
    texts = [t for t in (clean(p) for p in _PARAGRAPH_RE.split(raw.strip())) if t and t != REJECT]
    if len(texts) < 2 and parts > 1:
        sentences = [s for s in _SENTENCE_RE.split(whole) if s]
        size = max(2, -(-len(sentences) // parts))
        texts = [" ".join(sentences[i:i + size]) for i in range(0, len(sentences), size)]
    if len(texts) > parts:
        texts = [*texts[:parts - 1], " ".join(texts[parts - 1:])]
    return texts


def _artist_talk(m: Material) -> str:
    return ("다음에 나갈 곡의 아티스트나 장르에 대해 이야기하는 멘트를 3문장 이내로 작성하라. "
            "곡 제목은 아직 말하지 않는다.\n" + data_block(TRACK, track_line(m.items[0])))


def _music_intro(m: Material) -> str:
    assert m.track is not None
    # 곡 제목·아티스트는 외부 텍스트다 (YouTube 메타데이터, F-34)
    text = "다음 곡을 소개하는 멘트를 2문장 이내로 작성하라."
    if nickname := m.track.get("requester_nickname"):
        text += " 청취자의 신청곡임을 닉네임과 함께 소개한다."
        info = track_line(m.track) + "\n신청자: " + nickname
        if comment := m.track.get("requester_comment"):
            text += " 신청자가 남긴 한마디도 언급한다."
            info += "\n한마디: " + comment
        return text + "\n" + data_block(TRACK, info)
    return text + "\n" + data_block(TRACK, track_line(m.track))


def _story(m: Material) -> str:
    story = m.items[0]
    return ("청취자 사연을 읽고 DJ의 이야기를 덧붙이는 멘트를 4문장 이내로 작성하라. "
            "사연은 보낸 분의 닉네임과 함께 소개한다.\n"
            + data_block(STORY, f"닉네임: {story.get('nickname', '익명')}\n{story['body']}"))


def _text_read(m: Material) -> str:
    msg = m.items[0]
    # 낭독이다 — 풀어 말하면 인용 검사(pipeline.quoted)에 걸려 되돌아간다 (CORNER_DESIGN 5.3절)
    return ("청취자가 보낸 짧은 문자를 닉네임과 함께 소개한다. 문자 내용은 고치거나 요약하지 말고 "
            "그대로 읽은 뒤, 한두 문장으로 리액션하라.\n"
            + data_block(STORY, f"닉네임: {msg.get('nickname', '익명')}\n{msg['body']}"))


# DJ 대 청취자 코너 (CORNER_DESIGN 7.6절, #279) — 항목은 라운드의 지금 상태다 (inputs/votes.py view)
# DJ의 선택은 결과를 공개하는 멘트에서만 밝힌다. 그 전에 새면 청취자가 따라 고른다
HIDE_PICK_RULE = "DJ가 어느 쪽을 골랐는지는 아직 밝히지 않고, 짐작할 수 있는 말도 하지 않는다."
_OUTCOME_RULES = {
    "match": "DJ의 선택과 청취자 다수의 선택이 같았다. DJ가 맞혔다고 말한다.",
    "mismatch": "DJ의 선택과 청취자 다수의 선택이 갈렸다. 이번에는 청취자가 이겼다고 말한다.",
    "tie": "표가 똑같이 갈렸다. 무승부라고 말한다.",
    "no_votes": "표가 들어오지 않았다. 표 수는 말하지 않고, 이번 고민은 무승부로 넘어간다고 말한다.",
    "late": "집계가 늦어져 결과를 전하지 못한다. 결과를 지어내지 않고 DJ의 선택만 밝히며, 점수는 말하지 않는다.",
}


def _dilemma_block(vote: dict) -> str:
    options = " / ".join(vote.get("options", []))
    return data_block(DILEMMA, f"{vote.get('question', '')}\n선택지: {options}")


def _game_ask(m: Material) -> str:
    vote = m.items[0]
    return ("DJ와 청취자가 같은 쪽을 고르는지 겨루는 코너다. 아래 고민과 두 선택지를 소개하고 청취자에게 "
            "투표를 권하는 멘트를 3문장 이내로 작성하라. 두 선택지는 적힌 말 그대로 부르고, 투표는 화면에서 "
            f"할 수 있다고 알린다. {HIDE_PICK_RULE}\n" + _dilemma_block(vote))


def _game_talk(m: Material) -> str:
    vote = m.items[0]
    options = vote.get("options") or ["", ""]
    turn = int(vote.get("talks", 1))
    phase = vote.get("phase", "open")
    if phase == "closing":  # 이 멘트가 나갈 즈음 창이 닫힌다 — 권하지 않고 마감을 알린다
        return ("청취자 투표를 기다리며 이야기를 잇는 자리다. 이제 투표를 마감한다고 알리고, 결과를 확인하는 동안 "
                f"잠깐 기다려 달라고 말하는 멘트를 2문장 이내로 작성하라. 투표를 더 권하지 않는다. {HIDE_PICK_RULE} "
                "투표 결과를 짐작하거나 지어내지 않는다.\n" + _dilemma_block(vote))
    if phase == "closed":
        # 창은 닫혔고 결과 공개 멘트를 만드는 중이다. 결과가 청취자에게 들리기까지 25초쯤 걸린다(공개 멘트 생성 +
        # 방송 지연). "결과가 곧 나온다"고만 시켰더니 그 말이 서너 번 되풀이됐다 (2026-11-14 실제 LLM 10회)
        return ("청취자 투표를 기다리며 이야기를 잇는 자리다. 투표는 이미 마감됐다. 결과를 확인하는 동안, 아래 고민을 "
                "두고 떠오르는 장면 하나를 가볍게 이야기하는 멘트를 3문장 이내로 작성하라. 어느 한쪽 편을 들지 않고, "
                f"투표를 더 권하지 않는다. {HIDE_PICK_RULE} 결과가 곧 나온다는 말은 하지 않아도 된다. "
                "투표 결과를 짐작하거나 지어내지 않고, 직전 멘트와 같은 말을 되풀이하지 않는다.\n" + _dilemma_block(vote))
    text = "청취자 투표를 기다리며 이야기를 잇는 멘트를 3문장 이내로 작성하라. "
    if turn <= 2:  # 두 쪽을 한 번씩 편든다 — 한쪽만 말하면 DJ의 선택처럼 들린다
        text += f"아래 고민에서 '{options[(turn - 1) % 2]}' 쪽을 고르는 마음을 풀어 말한다. "
    elif turn % 2:  # 결과가 올 때까지 잇는다 — 권하는 말만 되풀이하지 않게 번갈아 한다
        text += "아직 고르지 못한 청취자에게 가볍게 투표를 권하고, 두 선택지를 다시 한 번 부른다. "
    else:
        text += ("아래 고민을 두고 흔히 망설이게 되는 순간을 가볍게 이야기한다. 어느 한쪽 편을 들지 않고, "
                 "두 선택지를 한 번씩 다시 부른다. ")
    text += (f"{HIDE_PICK_RULE} 투표 결과를 짐작하거나 지어내지 않고, 직전 멘트와 같은 말을 되풀이하지 않는다.\n")
    return text + _dilemma_block(vote)


def _game_reveal(m: Material) -> str:
    vote = m.items[0]
    options = vote.get("options") or ["", ""]
    outcome = vote.get("outcome", "late")
    text = ("DJ의 선택과 청취자 투표 결과를 공개하는 멘트를 6문장 이내로 짧게 작성하라. 먼저 DJ가 "
            f"'{options[int(vote.get('pick', 0))]}' 쪽을 골랐다고 밝히고 아래 까닭을 DJ의 말투로 한 문장으로 말한다. ")
    if outcome in ("match", "mismatch", "tie"):
        text += "이어서 아래 투표 결과의 표 수를 적힌 숫자 그대로 전한다. 숫자를 바꾸거나 지어내지 않는다. "
    text += _OUTCOME_RULES[outcome]
    if outcome != "late" and (score := vote.get("score")):
        text += f" 끝으로 지금 점수가 DJ {score['dj']}점, 청취자 {score['listeners']}점이라고 알린다."
    blocks = [data_block(DILEMMA, f"{vote.get('question', '')}\nDJ가 고른 까닭: {vote.get('reason', '')}")]
    if outcome in ("match", "mismatch", "tie"):
        first, second = vote.get("tally", (0, 0))
        blocks.append(data_block(VOTE_RESULT, f"{options[0]}: {first}표\n{options[1]}: {second}표"))
    return text + "\n" + "\n".join(blocks)


def _game_final(m: Material) -> str:
    score = m.extra.get("score") or {"dj": 0, "listeners": 0, "draw": 0}
    dj, listeners = score["dj"], score["listeners"]
    winner = ("DJ가 이겼다" if dj > listeners else "청취자가 이겼다" if listeners > dj else "비겼다")
    return (f"코너를 마치며 최종 점수를 알리는 멘트를 2문장 이내로 작성하라. DJ {dj}점, 청취자 {listeners}점으로 "
            f"{winner}고 숫자 그대로 말하고, 함께해 준 청취자에게 짧게 인사한다. 새 고민을 꺼내지 않는다.")


def _news_headline(m: Material) -> str:
    return ("다음 소식들의 헤드라인을 한 문장씩 전하라. 소재에 없는 사실은 말하지 않고, "
            "출처는 매체 이름으로 밝힌다.\n" + data_block(SOURCES, news_lines(m.items)))


def _news_item(m: Material) -> str:
    field = f"{m.label} 분야의 " if m.label else ""
    return (f"다음 {field}기사 하나를 두세 문장으로 요약해 전하라. 소재에 없는 사실은 말하지 않고, "
            "출처는 매체 이름으로 밝힌다.\n" + data_block(SOURCES, news_lines(m.items)))


def _news_recap(m: Material) -> str:
    titles = "\n".join(f"- {t}" for t in m.extra.get("covered", []))
    text = "이번에 전한 소식을 두 문장 이내로 정리하라. 새로운 사실은 덧붙이지 않는다."
    return text + ("\n" + data_block(SOURCES, titles) if titles else "")


def _closing_lookback(m: Material) -> str:
    text = "방송을 마무리하며 오늘 방송을 짧게 돌아보는 멘트를 3문장 이내로 작성하라."
    if aired := m.extra.get("lookback"):
        text += (" 아래 오늘 방송에 나온 청취자 이야기 중 한두 가지를 짧게 짚는다. 내용을 지어내거나 뜻을 바꾸지 않고, "
                 "닉네임은 블록에 있을 때만 부른다.\n"
                 + "\n".join(data_block(CALLBACK, a.text()) for a in aired))
    if covered := m.extra.get("covered_angles"):
        text += "\n아래는 오늘 나눈 이야기 갈래다. 돌아볼 때 참고만 하고 목록처럼 읽지 않는다.\n" + data_block(
            COVERED_ANGLES, "\n".join(f"- {t}" for t in covered))
    return text


def _closing_farewell(m: Material) -> str:
    text = "마지막 곡을 소개하고 끝인사를 하는 멘트를 3문장 이내로 작성하라."
    return text + ("\n" + data_block(TRACK, track_line(m.track)) if m.track else "")


def _filler(m: Material) -> str:
    return _with_topic("곡 사이를 잇는 짧은 브릿지 멘트를 1~2문장으로 작성하라.", m)


@dataclass(frozen=True)
class StepKind:
    marker: str  # 지시문에 반드시 들어가는 구절 — 더미 LLM의 단계 식별 표식
    build: Callable[[Material], str]
    seconds: tuple[int, int]  # 예상 발화 길이 [예시] — 평가 도구의 길이 경고 기준, 실측 후 조정
    quotable: bool = False  # 채팅 인용을 붙일 수 있는 단계 (코너 계약이 인용을 허용할 때만 붙는다)


STEP_KINDS: dict[str, StepKind] = {
    "opening_greeting": StepKind("방송을 시작한다", _opening_greeting, (15, 40)),
    "topic_intro": StepKind("오늘의 주제를 소개", _topic_intro, (8, 25)),
    "corner_preview": StepKind("코너를 예고", _corner_preview, (10, 30), quotable=True),
    "talk_open": StepKind("이야기를 꺼내는", _talk_open, (10, 30), quotable=True),
    "talk_resume": StepKind("다시 오늘의 이야기로 돌아온", _talk_resume, (10, 30), quotable=True),
    "talk": StepKind("이야기를 이어가는", _talk, (15, 45), quotable=True),
    # 덩어리의 문단 하나가 세그먼트 하나다 — 길이 기준은 문단에 건다. 채팅은 덩어리 뒤의 이야기 단계가 인용한다
    BLOCK_STEP: StepKind(BLOCK_MARKER, _talk_block, (8, 25)),
    "artist_talk": StepKind("아티스트나 장르", _artist_talk, (10, 30), quotable=True),
    "music_intro": StepKind("다음 곡을 소개", _music_intro, (10, 25), quotable=True),
    "story": StepKind("사연을 읽고", _story, (20, 60), quotable=True),
    "text_read": StepKind("짧은 문자를", _text_read, (8, 25)),
    "game_ask": StepKind("투표를 권하는", _game_ask, (10, 30)),
    "game_talk": StepKind("투표를 기다리며", _game_talk, (10, 30), quotable=True),
    "game_reveal": StepKind("투표 결과를 공개", _game_reveal, (15, 40)),
    "game_final": StepKind("최종 점수", _game_final, (8, 20)),
    "news_headline": StepKind("헤드라인을", _news_headline, (15, 45)),
    "news_item": StepKind("기사 하나를", _news_item, (20, 60)),
    "news_recap": StepKind("전한 소식을", _news_recap, (10, 30)),
    "closing_lookback": StepKind("돌아보는", _closing_lookback, (10, 30), quotable=True),
    "closing_farewell": StepKind("끝인사", _closing_farewell, (10, 30)),
    "filler": StepKind("브릿지", _filler, (5, 15)),
}


def build_prompt(step: str, material: Material, profile: StationProfile, *,
                 plain: bool = False) -> Prompt:
    kind = STEP_KINDS[step]
    user = kind.build(material)
    if material.quotes:
        if not kind.quotable:
            raise ValueError(f"{step} 단계는 채팅을 인용하지 않는다")
        quotes, steer = list(material.quotes), material.extra.get("steer")
        rule = QUOTE_RULE
        if len(quotes) > 1:
            rule += f" {MULTI_QUOTE_RULE}"
            if steer:  # 화제가 될 채팅(첫 건)을 마지막에 인용해야 "이 얘기를 더"로 이어진다
                quotes = [*quotes[1:], quotes[0]]
        if steer:
            rule += f" {MULTI_STEER_RULE if len(quotes) > 1 else STEER_RULE}"
        if material.feature_ref:
            rule += f" {WITH_FEATURE_RULE}"
        if material.track and (len(quotes) > 1 or material.feature_ref):
            rule += f" {TRACK_LAST_RULE}"
        chats = "\n".join(q.body for q in quotes)
        user += f"\n{rule}\n" + data_block(CHAT, chats)
    if trend := material.extra.get("aggregate"):
        user += f"\n{AGGREGATE_RULE}\n" + data_block(TREND, trend.keyword)
    elif preview := material.extra.get("preview"):
        user += "\n" + PREVIEW_RULE.format(title=preview["title"])
    return Prompt(system=system_prompt(profile, plain=plain), user=user)
