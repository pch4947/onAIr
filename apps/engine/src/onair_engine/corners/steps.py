"""단계별 프롬프트 빌더 — 코너는 설정(config/formats)이 정하고, 단계 유형마다 지시문은 여기서 만든다.

외부 텍스트(곡 정보, 기사, 사연, 청취자 채팅, 오늘의 주제)는 전부 데이터 블록으로 넣는다 (prompt_data.py).
지시문마다 marker 구절이 하나씩 들어간다 — 더미 LLM이 프롬프트만 보고 단계를 알아보는 표식이다
(pipeline/llm.py detect_step). 지시문을 고칠 때 marker를 지우지 않는다 (test_steps가 검사한다).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..domain import Material, Prompt, StationProfile
from ..prompt_data import (
    ANGLE,
    CHAT,
    CORNERS,
    COVERED_ANGLES,
    NOW,
    SOURCES,
    STORY,
    TOPIC,
    TRACK,
    TREND,
    data_block,
)
from .base import system_prompt

# 채팅 인용 지시 — 인용할지 말지를 LLM에 맡기지 않는다. 넣었으면 반드시 인용한다 (CORNER_DESIGN 5.1절)
QUOTE_RULE = ("아래 청취자 채팅을 '~라고 하신 분이 있네요'처럼 짧게 인용하고 한 문장으로 반응한다. "
              "닉네임이나 아이디는 말하지 않는다.")
# 다음 단계부터 이 채팅이 화제가 된다 — 청취자가 흐름이 바뀐 이유를 알 수 있게 한다 (CORNER_DESIGN 5.2절)
STEER_RULE = "그리고 이 얘기를 조금 더 해 보겠다고 자연스럽게 말한다."
# 몰린 입력은 개인이 아니라 집계로 인정한다 — 특정 채팅을 인용하거나 건수를 지어내지 않는다 (9.2절)
AGGREGATE_RULE = ("멘트 첫머리에 지금 채팅창에서 아래 화제 얘기가 많이 오간다고 한 문장으로 언급한다. "
                  "특정 채팅을 인용하거나 숫자를 말하지 않는다.")
PREVIEW_RULE = ("멘트 끝에 사연 문자가 많이 와 있으니 이따 '{title}' 시간에 읽겠다고 "
                "한 문장으로 알린다. 숫자를 말하지 않는다.")
# 시간 맥락은 화제를 꺼내는 단계에만 붙인다 — 매 멘트에 붙이면 "벌써 자정이네요"가 되풀이된다
TIME_RULE = "아래 지금 시각은 이야기와 어울릴 때만 짧게 쓰고, 꼭 말하지 않아도 된다."


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


def _opener_context(m: Material) -> str:
    """화제를 꺼내는 단계의 덧붙임 — 질문, 이미 다룬 갈래, 지금 시각."""
    text = ""
    if m.extra.get("question"):
        text += " 끝에 청취자에게 가볍게 질문을 하나 던진다."
    if covered := m.extra.get("covered_angles"):
        text += (" 아래 이미 다룬 갈래는 되풀이하지 않는다.\n"
                 + data_block(COVERED_ANGLES, "\n".join(f"- {t}" for t in covered)))
    if now := m.extra.get("now"):
        text += f"\n{TIME_RULE}\n" + data_block(NOW, now)
    return text


def _talk_open(m: Material) -> str:
    text = "오늘의 주제로 이야기를 꺼내는 멘트를 3문장 이내로 작성하라."
    if m.extra.get("angle"):
        # 오프닝(topic_intro)이 주제를 이미 소개했다 — 2026-11-07 실행에서 바로 뒤에 한 번 더 소개했다
        text += " 주제는 오프닝에서 이미 소개했으니 다시 소개하지 않고, 아래 이번 갈래로 바로 시작한다."
    return _with_topic(text + _opener_context(m), m) + _angle(m)


def _talk_resume(m: Material) -> str:
    # 직전에 접착 문구(resume)가 "다시 … 돌아왔어요"를 이미 말했다
    text = ("다른 순서를 지나 다시 오늘의 이야기로 돌아온 멘트를 3문장 이내로 작성하라. "
            "주제를 처음 소개하듯 말하지 않고, 돌아왔다는 말도 하지 않는다.")
    text += (" 아래 이번 갈래로 화제를 옮긴다." if m.extra.get("angle")
             else " 앞에서 한 말과 다른 면을 꺼낸다.")
    return _with_topic(text + _opener_context(m), m) + _angle(m)


def _talk(m: Material) -> str:
    text = "지금 화제로 이야기를 이어가는 멘트를 3문장 이내로 작성하라. 직전 멘트와 같은 말을 되풀이하지 않는다."
    if m.extra.get("angle"):
        text += " 아래 이번 갈래의 말할 거리 중 직전 멘트에서 아직 하지 않은 것을 하나 골라 말한다."
    return _with_topic(text, m) + _angle(m)


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


def _game_round(m: Material) -> str:
    vote = m.items[0]
    options = " / ".join(vote.get("options", []))
    return ("투표 주제와 선택지를 소개하고 청취자에게 투표를 권하는 멘트를 3문장 이내로 작성하라.\n"
            + data_block(STORY, f"{vote.get('question', '')}\n선택지: {options}"))


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
    return "방송을 마무리하며 오늘 방송을 짧게 돌아보는 멘트를 3문장 이내로 작성하라."


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
    "artist_talk": StepKind("아티스트나 장르", _artist_talk, (10, 30), quotable=True),
    "music_intro": StepKind("다음 곡을 소개", _music_intro, (10, 25), quotable=True),
    "story": StepKind("사연을 읽고", _story, (20, 60), quotable=True),
    "text_read": StepKind("짧은 문자를", _text_read, (8, 25)),
    "game_round": StepKind("투표를 권하는", _game_round, (15, 40)),
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
        chats = "\n".join(q.body for q in material.quotes)
        rule = f"{QUOTE_RULE} {STEER_RULE}" if material.extra.get("steer") else QUOTE_RULE
        user += f"\n{rule}\n" + data_block(CHAT, chats)
    if trend := material.extra.get("aggregate"):
        user += f"\n{AGGREGATE_RULE}\n" + data_block(TREND, trend.keyword)
    elif preview := material.extra.get("preview"):
        user += "\n" + PREVIEW_RULE.format(title=preview["title"])
    return Prompt(system=system_prompt(profile, plain=plain), user=user)
