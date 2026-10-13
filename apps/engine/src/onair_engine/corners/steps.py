"""단계별 프롬프트 빌더 — 코너는 설정(config/formats)이 정하고, 단계 유형마다 지시문은 여기서 만든다.

외부 텍스트(곡 정보, 기사, 사연, 청취자 채팅, 오늘의 주제)는 전부 데이터 블록으로 넣는다 (prompt_data.py).
지시문마다 marker 구절이 하나씩 들어간다 — 더미 LLM이 프롬프트만 보고 단계를 알아보는 표식이다
(pipeline/llm.py detect_step). 지시문을 고칠 때 marker를 지우지 않는다 (test_steps가 검사한다).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..domain import Material, Prompt, StationProfile
from ..prompt_data import CHAT, CORNERS, SOURCES, STORY, TOPIC, TRACK, data_block
from .base import system_prompt

# 채팅 인용 지시 — 인용할지 말지를 LLM에 맡기지 않는다. 넣었으면 반드시 인용한다 (CORNER_DESIGN 5.1절)
QUOTE_RULE = ("아래 청취자 채팅을 '~라고 하신 분이 있네요'처럼 짧게 인용하고 한 문장으로 반응한다. "
              "닉네임이나 아이디는 말하지 않는다.")


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


def _talk_open(m: Material) -> str:
    text = "오늘의 주제로 이야기를 꺼내는 멘트를 3문장 이내로 작성하라."
    if m.extra.get("question"):
        text += " 끝에 청취자에게 가볍게 질문을 하나 던진다."
    return _with_topic(text, m)


def _talk(m: Material) -> str:
    return _with_topic("지금 화제로 이야기를 이어가는 멘트를 3문장 이내로 작성하라. "
                       "직전 멘트와 같은 말을 되풀이하지 않는다.", m)


def _artist_talk(m: Material) -> str:
    return ("다음에 나갈 곡의 아티스트나 장르에 대해 이야기하는 멘트를 3문장 이내로 작성하라. "
            "곡 제목은 아직 말하지 않는다.\n" + data_block(TRACK, track_line(m.items[0])))


def _music_intro(m: Material) -> str:
    assert m.track is not None
    # 곡 제목·아티스트는 외부 텍스트다 (YouTube 메타데이터, F-34)
    return "다음 곡을 소개하는 멘트를 2문장 이내로 작성하라.\n" + data_block(TRACK, track_line(m.track))


def _story(m: Material) -> str:
    story = m.items[0]
    return ("청취자 사연을 읽고 DJ의 이야기를 덧붙이는 멘트를 4문장 이내로 작성하라. "
            "사연은 보낸 분의 닉네임과 함께 소개한다.\n"
            + data_block(STORY, f"닉네임: {story.get('nickname', '익명')}\n{story['body']}"))


def _text_read(m: Material) -> str:
    msg = m.items[0]
    return ("청취자가 보낸 짧은 문자를 닉네임과 함께 읽고 한두 문장으로 리액션하라.\n"
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
        user += f"\n{QUOTE_RULE}\n" + data_block(CHAT, chats)
    return Prompt(system=system_prompt(profile, plain=plain), user=user)
