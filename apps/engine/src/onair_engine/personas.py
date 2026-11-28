"""persona 초안 생성·검증 — 방 생성 화면 (docs/persona.md 3절, 계약 6.2절).

LLM을 쓰는 시점을 방 생성 한 번으로 묶는다. 같은 폼 선택지는 규칙으로 같은 기본 말투가 되고,
LLM은 이름·컨셉·말투 묘사·예시 멘트·입버릇·금지 사항만 만든다. 방송 중에는 저장된 값만 쓴다.
"""
from __future__ import annotations

import json
import re

from onair_schema import Persona, PersonaForm, Style, error_detail
from pydantic import ValidationError

from .domain import Prompt
from .eval.checks import leaked_markup, level_violations
from .pipeline.llm import LlmClient
from .pipeline.safety import SafetyChecker
from .prompt_data import DATA_RULES, GENRES, HOST_NOTE, data_block

# style → tone 기본 문장 (규칙). 실험용 방에서 같은 선택지가 같은 기본 말투가 되게 한다
_ENERGY = {"low": "차분하고 느린 호흡의", "mid": "편안하고 자연스러운 호흡의",
           "high": "밝고 경쾌한 호흡의"}
_FORMALITY = {"polite": "존댓말", "casual": "반말"}
_HUMOR = {"rare": "농담은 드물게 한다", "some": "가끔 가벼운 농담을 섞는다",
          "often": "농담을 자주 섞는다"}
_LEVEL = {"polite": "존댓말(해요체)만 쓰고 반말을 섞지 않는다",
          "casual": "반말만 쓰고 존댓말을 섞지 않는다"}

TONE_MAX = 100  # Persona.tone 상한 — 스키마와 같게
_TEXT_FIELDS = ("dj_name", "concept", "tone", "examples", "signature_phrases", "forbidden")


def base_tone(style: Style) -> str:
    """예: polite + low + rare → '차분하고 느린 호흡의 존댓말. 농담은 드물게 한다.'"""
    return f"{_ENERGY[style.energy]} {_FORMALITY[style.formality]}. {_HUMOR[style.humor]}."


class FormRejected(ValueError):
    """폼 자체를 쓸 수 없다 — 422. field는 요청 본문 기준 경로다."""

    def __init__(self, field: str, reason: str):
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


class DraftsFailed(Exception):
    """재생성까지 했지만 쓸 만한 초안이 하나도 없다 — 503.

    RuntimeError를 상속하지 않는다 — API가 RuntimeError를 "LLM을 못 만듦(키 누락)"으로 읽는다.
    """


_SYSTEM = (
    "당신은 라디오 DJ 캐릭터를 설계하는 작가다. 호스트가 고른 조건으로 DJ persona 초안을 만든다. "
    "흔한 감성 라디오의 DJ가 아니라, 말하는 방식으로 기억되는 사람을 만든다. "
    "결과는 그대로 방송 대본 생성 프롬프트에 들어가므로, 소리 내어 읽을 수 있는 한국어로만 쓴다. "
    "마크다운·이모지·괄호 지문을 쓰지 않는다. 실존 인물·브랜드·개인정보·정치·종교 의견을 넣지 않는다. "
    + DATA_RULES
    + " 출력은 JSON 객체 하나뿐이다. 설명이나 코드 블록 표시 없이 JSON만 출력한다."
)


# 장르를 여럿 고른 방 — 초안이 장르를 하나씩 나눠 가지면 컨셉("제이팝으로 여는 오후")이 선곡을 그 장르로 묶는다.
# 선곡과 신청곡 판정이 컨셉을 같이 읽기 때문이다 (inputs/song_picks.py, inputs/song_fit.py)
GENRE_RULES = [
    "아래 선곡 취향은 이 방송에서 트는 장르 전부다. 어느 초안이든 이 장르를 모두 튼다.",
    "- 초안마다 장르를 하나씩 나눠 맡기지 않는다. 초안은 장르가 아니라 분위기와 DJ의 성격으로 가른다.",
    "- concept, tone_detail, examples에 장르 이름을 쓰지 않고, 한 장르만 트는 방송처럼 말하지 않는다.",
]
# 폼의 장르 이름과 다르게 적는 표기 — 컨셉에 장르가 들어갔는지 볼 때 같이 찾는다
_GENRE_ALIASES = {"j-pop": ("제이팝", "j팝", "jpop"), "r&b": ("알앤비",), "edm": ("이디엠", "전자음악")}


def genres_named(text: str, genres: list[str]) -> list[str]:
    """text에 나온 장르 이름. '힙합/랩'은 두 낱말로 보고, 한 글자 이름('팝')은 다른 낱말의 끝('제이팝')이면 세지 않는다."""
    found = []
    for genre in genres:
        for word in filter(None, (part.strip() for part in genre.split("/"))):
            for name in (word, *_GENRE_ALIASES.get(word.lower(), ())):
                pattern = re.escape(name) if len(name) > 1 else rf"(?<![0-9A-Za-z가-힣]){re.escape(name)}"
                if re.search(pattern, text, re.IGNORECASE):
                    found.append(name)
    return found


def _keeps_every_genre(form: PersonaForm) -> bool:
    """컨셉을 한 장르로 좁히면 안 되는 폼 — 장르를 여럿 골랐고, 호스트 메모가 그 가운데 하나를 짚지 않았다."""
    genres = list(form.music_taste)
    return len(genres) > 1 and not genres_named(form.host_note or "", genres)


# 초안의 말투 묘사와 예시 멘트는 매 멘트의 시스템 프롬프트에 들어가 대본의 말투를 가장 강하게 정한다 (corners/base.py).
# 예전에는 "말투 묘사 한 문장", "이 DJ가 실제로 할 법한 라디오 멘트 5개"만 요구했다. 사용자 입력을 흉내 낸 방 30개로
# 만든 DJ의 예시 멘트 150개 가운데 59개(39%)에 위로하는 말이 들었고, 차분한 DJ 일곱 명 가운데 셋의 입버릇이
# "천천히 들어봐요"류였으며, 말투 묘사는 "빗소리와 노래 사이에 위로를 놓아드려요" 같은 분위기 문장이었다(#377).
# 본보기 문장은 적지 않는다 — 적으면 모든 초안이 그 문장을 닮는다 (T-007)
# 사용자가 짚은 말투 묘사 — "빗방울을 세듯 천천히 말을 이어가", "책장을 넘기듯 여백을 두며 작은 농담을 건넨다",
# "골목 모퉁이처럼 살짝만 드러낸다". 두루뭉술한 비유와 분위기 말은 대본에서 확인할 수 없고, 시스템 프롬프트에 들어가
# 대본을 서정적으로 끈다. 프롬프트로 막고 코드로도 거른다(vague_detail). (#377)
# 처음에는 적을 행동을 "문장을 어떻게 끊는지, 글에서 무엇에 먼저 반응하는지, 질문을 어떻게 하는지, 무엇에 웃는지"로
# 못 박았다. 그러자 초안 86개의 묘사가 거의 모두 "문장을 짧게 끊고, 청취자 글의 …부터 짚고, 질문은 하나씩 한다"가 됐다.
# 무엇을 적을지는 DJ마다 고르게 한다
TONE_DETAIL_RULE = ("풍경이나 분위기가 아니라 이 DJ가 방송에서 실제로 하는 행동을 글자 그대로 적는다 — 문장의 길이, 자주 하는 "
                    "반응, 무엇에 웃는지, 청취자를 어떻게 대하는지 가운데 이 DJ를 다른 DJ와 가르는 것을 고른다. 대본을 읽으면 "
                    "그렇게 했는지 확인할 수 있는 행동이어야 한다. 비유는 쓰지 않는다 — 다른 것에 빗대는 말('처럼', '듯', "
                    "'같은')도, 뜻을 옮겨 쓴 동사도 쓰지 않고 낱말을 글자 그대로의 뜻으로만 쓴다. 풍경, 날씨, 사물, 분위기, "
                    "느낌을 나타내는 말도 쓰지 않는다. 초안마다 서로 다른 행동을 적는다. "
                    # 행동을 적게 하자 몸짓과 한 가지 소재에 매인 행동이 나왔다 — 힙합 방 초안 36개 가운데 "박자가 바뀌는 곳에서
                    # 손가락으로 수를 세고", "손뼉으로 리듬을 세어 준다", "박자를 입으로 짧게 흉내 내고". 이 묘사는 매 멘트에
                    # 들어가므로 DJ가 오프닝부터 곡 소개까지 "손가락으로 하나, 둘, 셋, 넷"을 되풀이했다 (#388)
                    "말로 하는 행동만 적는다 — 손짓, 몸짓, 표정, 목소리 크기처럼 대본의 글자로 드러나지 않는 것은 적지 "
                    "않는다. 무엇을 다루는지(곡의 박자, 가사, 악기 같은 소재)가 아니라 어떻게 말하는지를 적는다 — 주제 "
                    "이야기, 청취자 글, 곡 소개 어느 멘트에서나 할 수 있는 행동이어야 하고, 한 가지 소재가 있어야만 할 수 "
                    "있는 행동은 적지 않는다.")
_COMPARISON = re.compile(r"처럼|마냥|(?<!반)듯")
_LIKE = re.compile(r"([가-힣]+)(\s?)같([은이])")
# "청취자를 같은 편으로", "청취자와 같이 웃는다", "끌어들여 같이 떠든다", "늘 같은 말"은 빗댄 말이 아니다 —
# 조사나 부사 뒤의 '같은/같이', 띄어 쓴 '같이'(함께)는 넘긴다
_NOT_LIKE_ENDS = "을를이가은는와과랑도에로고"
_NOT_LIKE_WORDS = {"늘", "항상", "매번", "다", "또", "똑", "모두", "함께", "서로"}
# 대본의 글자로 드러나지 않는 것 — DJ의 몸짓과 목소리 크기. 적혀 있으면 대본이 그 몸짓을 말로 중계한다 (#388)
UNSPOKEN_WORDS = ("손가락", "손뼉", "박수", "손짓", "손으로", "몸짓", "고개", "어깨", "표정", "미소", "눈빛", "목소리",
                  "입으로", "발을", "발로")
VAGUE_WORDS = ("여백", "여운", "온기", "온도", "포근", "살며시", "풍경", "창밖", "빗소리", "빗방울", "달빛", "별빛", "밤공기",
               "골목", "책장", "담요", "잔잔", "은은", "스며", "물들", "감싸", "어루만", "건져", "갈림길", "올라타", "숨결",
               "쉼표", "얹")


def vague_detail(text: str) -> list[str]:
    """말투 묘사에 든 빗댄 말과 분위기 낱말. 비어 있으면 통과."""
    found = _COMPARISON.findall(text)
    found += [m.group(0) for m in _LIKE.finditer(text)
              if m.group(1)[-1] not in _NOT_LIKE_ENDS and m.group(1) not in _NOT_LIKE_WORDS
              and not (m.group(3) == "이" and m.group(2))]
    return found + [word for word in VAGUE_WORDS if word in text]


def unspoken_detail(text: str) -> list[str]:
    """말투 묘사에 든 몸짓·목소리 낱말. 비어 있으면 통과."""
    return [word for word in UNSPOKEN_WORDS if word in text]


APART_RULE = ("같은 조건에서도 무엇에 반응하고, 어떻게 웃고, 청취자를 어떻게 대하는지가 다른 세 사람이다 — "
              "분위기만 다르고 말하는 방식이 같은 초안은 만들지 않는다.")
# 처음 문구로는 둘째 예시 89개 가운데 52개가 "보내주신 글"로 시작했다 — 예시는 매 멘트에 들어가 그 첫머리가 대본에 샌다
EXAMPLE_SITUATIONS = ("어떤 일을 두고 자기 생각이나 취향을 말할 때",
                      "청취자가 보낸 글에 반응할 때(글을 받았다는 말로 시작하지 않고 글의 내용에 바로 반응한다)",
                      "이 DJ다운 반응(감탄, 농담, 맞장구 같은 것)을 할 때", "다음 곡을 소개할 때", "방송을 마무리할 때")
# 구호·표어를 막고 말버릇 묘사로 바꾸자 텐션 높은 DJ의 예시에서 에너지까지 빠졌다 — 예시의 느낌표 32% → 17%, 그 DJ로
# 방송한 대본의 느낌표 13~52% → 0~8%. 예시가 차분하면 대본도 차분하다. 에너지가 예시에 그대로 들리게 한다
EXAMPLE_ENERGY = {
    "high": ("다섯 개 모두 텐션이 그대로 들려야 한다 — 짧게 끊어 치는 문장, 감탄, 크게 반응하는 말을 아끼지 않는다. "
             "차분하게 설명하는 문장으로 쓰지 않는다"),
    "mid": "다섯 개 모두 편하게 수다 떠는 호흡이 들려야 한다",
    # "서두르지 않는"이라고 썼더니 입버릇 "천천히 볼게요"가 다섯 명에게 생겼다 — 빠르기를 말하는 낱말을 쓰지 않는다
    "low": "다섯 개 모두 낮고 차분한 목소리가 들려야 한다 — 차분함은 꾸민 말이 아니라 짧고 담담한 문장으로 드러낸다",
}
DRAFT_RULES = [
    # "무엇을 하는 방송인지 드러나는 한 구절"이라고만 했더니 89개 가운데 86개가 "~하는 방송"으로 끝났다
    ("- concept: 프로그램 이름처럼 부를 수 있는 짧은 구절, 30자 이내. 무엇을 하는 방송인지 짐작되게 하되 "
     "'~하는 방송' 꼴로 끝내지 않는다. 시처럼 꾸민 수식어는 쓰지 않는다"),
    ("- examples: 이 DJ가 방송에서 실제로 할 말 정확히 5개, 각 80자 이내, 한두 문장. 말투 규칙을 반드시 지킨다. "
     "다섯 개는 차례로 이런 상황이다 — "
     + ", ".join(f"{n}) {s}" for n, s in enumerate(EXAMPLE_SITUATIONS, 1))
     # 소재를 정하지 않았더니 초안 87개의 예시가 같은 몇 가지 생활 소재에 몰렸다
     + ". 다섯 개는 서로 다른 소재로 쓰고, 세 초안끼리도 같은 소재를 되풀이하지 않는다"),
    ("- examples는 대본의 말투를 정하는 본보기다. 위로하거나 다독이는 문장은 다섯 개 가운데 하나까지만 쓴다 — 호스트 "
     "메모가 위로해 주는 DJ를 바라면 그 성격을 따르되, 정해진 위로의 말을 되풀이하지 않고 상대의 일을 짚는 구체적인 "
     "말로 위로한다. "
     # "사물이나 감정을 사람처럼 말하는 표현은 쓰지 않는다"고만 했을 때는 농담 많은 DJ의 예시에 그런 농담이 그대로
     # 나왔다("통장이 동의하지 않네", "냉장고가 회의실인 줄 알았나 봐"). 대본 쪽과 같은 꼴로 막는다 (#376, T-023)
     "풍경이나 감정을 시처럼 꾸미는 말은 쓰지 않는다. 농담하는 문장의 주어는 사람이다 — 물건, 음식, 감정, 요일, "
     "할 일 같은 것을 주어로 세워 무엇을 한다고 말하며 웃기지 않는다. "
     "곡 제목, 가수 이름, 청취자의 닉네임은 넣지 않는다"),
    ("- signature_phrases: 이 DJ가 문장 안에서 자주 쓰는 짧은 말버릇 0~2개, 각 15자 이내. 앞뒤 문장에 이어 붙여 쓰는 "
     "말이어야 한다 — 멘트 끝에 붙이는 구호나 표어, 위로하는 말, 쉬어 가자거나 느리게 하자고 권하는 말은 "
     "입버릇으로 만들지 않는다"),
    "- forbidden: 이 캐릭터가 하지 않는 것 2~4개, 각 40자 이내",
]


def build_prompt(form: PersonaForm, count: int, avoid_names: list[str]) -> Prompt:
    """초안 생성 프롬프트. 더미 LLM이 '초안 개수:', '기본 말투:', '고정 이름:', '피할 이름:' 줄을 읽는다."""
    tone = base_tone(form.style)
    detail_max = TONE_MAX - len(tone) - 1
    name_rule = (f"고정 이름: {form.dj_name}\n모든 초안의 dj_name은 위 이름을 그대로 쓴다."
                 if form.dj_name else
                 "dj_name은 초안마다 다르게, 2~4글자의 부르기 쉬운 한국어 이름으로 제안한다.")
    lines = [
        f"초안 개수: {count}",
        f"기본 말투: {tone}",
        f"말투 규칙: {_LEVEL[form.formality]}",
        name_rule,
    ]
    if avoid_names:
        lines.append(f"피할 이름: {', '.join(avoid_names)}")
    lines += [
        "",
        "아래 형식의 JSON을 출력한다.",
        ('{"drafts": [{"dj_name": "...", "concept": "...", "tone_detail": "...", '
         '"examples": ["...", "...", "...", "...", "..."], "signature_phrases": ["..."], '
         '"forbidden": ["..."]}]}'),
        "",
        DRAFT_RULES[0],
        f"- tone_detail: 기본 말투에 덧붙일 행동 묘사 한 문장, {detail_max}자 이내. {TONE_DETAIL_RULE}",
        *DRAFT_RULES[1:],
        f"- examples의 에너지: {EXAMPLE_ENERGY[form.energy]}",
        f"- 초안 {count}개는 이름·컨셉이 겹치지 않고 성격이 서로 다르다. {APART_RULE}",
    ]
    if form.music_taste:
        lines.append("")
        if _keeps_every_genre(form):
            lines += GENRE_RULES
        lines.append(data_block(GENRES, ", ".join(form.music_taste)))
    if form.host_note:
        # 위로를 줄이는 규칙이 메모를 눌렀다 — "따뜻하게 위로해주는 언니 같은 느낌"을 적은 방의 세 초안이 모두 위로하지
        # 않는 DJ가 됐다(#377 첫 시도). 메모가 바라는 성격이 먼저다
        lines += ["", ("호스트가 원하는 DJ를 적은 메모다. 컨셉과 말투 묘사에 반영한다. 위의 규칙과 부딪치면 메모가 앞선다 — "
                       "세 초안 모두 메모가 바라는 성격을 지키고, 그 안에서 말하는 방식으로 갈린다 — 메모가 위로해 주는 DJ를 "
                       "바라면 세 초안 모두 청취자의 마음을 먼저 알아주는 사람이고, 해결책부터 내미는 사람으로 만들지 않는다. "
                       "말투 규칙과 금지 사항은 그대로 지킨다."),
                  data_block(HOST_NOTE, form.host_note)]
    return Prompt(system=_SYSTEM, user="\n".join(lines))


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_drafts(text: str) -> list[dict]:
    """LLM 출력에서 초안 목록을 꺼낸다. 코드 블록 표시나 앞뒤 설명이 붙어 와도 JSON 부분만 읽는다."""
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("JSON이 없습니다")
    data = json.loads(match.group(0))
    drafts = data.get("drafts") if isinstance(data, dict) else None
    if not isinstance(drafts, list):
        # 호출자는 "출력 해석 실패"를 ValueError 하나로 받는다 (json.JSONDecodeError도 ValueError)
        raise ValueError("drafts 배열이 없습니다")  # noqa: TRY004
    return [d for d in drafts if isinstance(d, dict)]


def _texts(persona: dict) -> list[tuple[str, str]]:
    """안전 검사할 (필드 경로, 텍스트) — 목록 필드는 원소마다."""
    out = []
    for name in _TEXT_FIELDS:
        value = persona.get(name)
        if isinstance(value, str):
            out.append((name, value))
        elif isinstance(value, list):
            out += [(f"{name}.{i}", v) for i, v in enumerate(value) if isinstance(v, str)]
    return out


def unsafe_fields(persona: dict, safety: SafetyChecker) -> list[tuple[str, str]]:
    """안전 규칙(L0)에 걸린 (필드 경로, 규칙) — 방 생성 화면의 검사와 방송 중 페르소나 전환이 같이 쓴다."""
    return [(field, rule) for field, text in _texts(persona) if (rule := safety.check_l0(text))]


class PersonaDrafter:
    def __init__(self, llm_factory, safety: SafetyChecker, *, max_tokens: int = 4000,
                 retries: int = 2):
        """llm_factory: LlmClient를 만드는 함수 — 키가 없어도 엔진이 뜨도록 첫 요청 때 만든다."""
        self._llm_factory = llm_factory
        self._llm: LlmClient | None = None
        self._safety = safety
        self.max_tokens = max_tokens
        self.retries = retries

    @property
    def llm(self) -> LlmClient:
        if self._llm is None:
            self._llm = self._llm_factory()  # RuntimeError(키·모델 누락)는 호출자가 503으로
        return self._llm

    async def drafts(self, form: PersonaForm, count: int) -> list[Persona]:
        if form.host_note and (rule := self._safety.check_l0(form.host_note)):
            raise FormRejected("form.host_note", f"안전 규칙에 걸렸습니다 ({rule})")
        valid: list[Persona] = []
        problems: list[str] = []
        # 실패한 초안만 다시 만든다 — 최대 retries회 (persona.md 3절 ③)
        for _ in range(1 + self.retries):
            need = count - len(valid)
            if need <= 0:
                break
            prompt = build_prompt(form, need, [p.dj_name for p in valid])
            text = await self.llm.complete(prompt, max_tokens=self.max_tokens)
            try:
                raw = parse_drafts(text)
            except ValueError as exc:
                problems.append(f"출력 해석 실패: {exc}")
                continue
            for item in raw[:need]:
                persona, problem = self._finish(form, item, {p.dj_name for p in valid})
                if persona is not None:
                    valid.append(persona)
                else:
                    problems.append(problem)
        if not valid:
            raise DraftsFailed("; ".join(problems[:3]) or "초안이 없습니다")
        return valid

    def _finish(self, form: PersonaForm, item: dict,
                taken: set[str]) -> tuple[Persona | None, str | None]:
        """LLM 초안 하나 → 검증된 Persona. 규칙으로 정할 값(style·voice·tone 앞부분)은 LLM 출력을 쓰지 않는다."""
        tone = base_tone(form.style)
        detail = str(item.get("tone_detail") or "").strip()
        candidate = {
            "style": form.style.model_dump(),
            "music_taste": list(form.music_taste),
            "voice": form.voice,
            "dj_name": form.dj_name or item.get("dj_name"),
            "concept": item.get("concept"),
            "tone": f"{tone} {detail}".strip(),
            "examples": item.get("examples"),
            "signature_phrases": item.get("signature_phrases") or [],
            "forbidden": item.get("forbidden") or [],
        }
        try:
            persona = Persona.model_validate(candidate)
        except ValidationError as exc:
            return None, error_detail(exc)
        if vague := vague_detail(detail):
            return None, f"tone_detail: 빗댄 말·분위기 낱말 {vague[:2]}"
        if unspoken := unspoken_detail(detail):
            return None, f"tone_detail: 대본에 드러나지 않는 몸짓·목소리 {unspoken[:2]}"
        if not form.dj_name and persona.dj_name in taken:
            return None, f"이름 중복: {persona.dj_name}"
        if _keeps_every_genre(form) and (named := genres_named(persona.concept, list(form.music_taste))):
            return None, f"concept: 고른 장르 가운데 하나만 적음 {named}"
        for field, text in _texts(persona.model_dump()):
            if rule := self._safety.check_l2(text):
                return None, f"{field}: 안전 규칙 ({rule})"
        if leaked := leaked_markup(" ".join(persona.examples)):
            return None, f"examples: 읽을 수 없는 기호 {leaked[:3]}"
        # 문장 경계가 사라지지 않게 마침표로 잇는다 — 입버릇은 문장부호 없이 오는 경우가 많다
        lines = [line.rstrip(" .") for line in [*persona.examples, *persona.signature_phrases]]
        if violations := level_violations(". ".join(lines) + ".", form.formality):
            return None, f"examples: 말투 이탈 {violations[:2]}"
        return persona, None

    def check(self, persona: dict) -> list[dict]:
        """호스트가 고친 최종본 — 스키마 + L0. 오류 목록 [{"field", "reason"}], 없으면 빈 목록."""
        try:
            Persona.model_validate(persona)
        except ValidationError as exc:
            return [{"field": "persona." + ".".join(str(p) for p in err["loc"]), "reason": err["msg"]}
                    for err in exc.errors()]
        return [{"field": f"persona.{field}", "reason": f"안전 규칙에 걸렸습니다 ({rule})"}
                for field, rule in unsafe_fields(persona, self._safety)]


# ── 더미 LLM용 초안 — API 키 없이 방 생성 화면을 개발·시연할 수 있게 한다 ─────────

_DUMMY_NAMES = ["새벽", "윤슬", "다온", "하람", "소라", "모래", "온새"]
_DUMMY_CONCEPTS = ["심야 스터디 라디오", "퇴근길 위로 라디오", "주말 오후 수다 라디오",
                   "비 오는 날 감성 라디오", "새벽 산책 라디오"]
_DUMMY_EXAMPLES = {
    "존댓말": ["오늘도 여기까지 오셨네요. 잠깐 숨 한 번 고르고 갈게요.",
             "창밖이 조용하죠. 이런 밤엔 피아노 소리가 잘 어울려요.",
             "보내 주신 이야기 잘 읽었어요. 마음이 많이 쓰이는 하루였겠어요.",
             "물 한 잔 따라 놓고 들어 주세요. 저도 지금 한 잔 마셨어요.",
             "다음 곡 들으면서 어깨 힘 한 번 빼 볼까요."],
    "반말": ["오늘도 왔구나. 잠깐 숨 한 번 고르고 가자.",
            "창밖이 조용하지. 이런 밤엔 피아노 소리가 딱이야.",
            "보내 준 이야기 잘 읽었어. 마음 많이 쓰인 하루였겠다.",
            "물 한 잔 따라 놓고 들어 줘. 나도 방금 한 잔 마셨어.",
            "다음 곡 들으면서 어깨 힘 한 번 빼 보자."],
}


def _field(prompt: Prompt, label: str) -> str | None:
    match = re.search(rf"^{label}: (.+)$", prompt.user, re.MULTILINE)
    return match.group(1).strip() if match else None


def dummy_drafts_json(prompt: Prompt) -> str:
    count = int(_field(prompt, "초안 개수") or 3)
    level = "반말" if "반말" in (_field(prompt, "기본 말투") or "") else "존댓말"
    fixed = _field(prompt, "고정 이름")
    avoid = set((_field(prompt, "피할 이름") or "").split(", "))
    names = [n for n in _DUMMY_NAMES if n not in avoid]
    drafts = []
    for i in range(count):
        drafts.append({
            "dj_name": fixed or names[i % len(names)],
            "concept": _DUMMY_CONCEPTS[i % len(_DUMMY_CONCEPTS)],
            "tone_detail": "문장을 짧게 끊어 말한다.",
            "examples": _DUMMY_EXAMPLES[level],
            "signature_phrases": ["천천히 가요"] if level == "존댓말" else ["천천히 가자"],
            "forbidden": ["청취자를 다그치기", "정치·종교에 대한 의견"],
        })
    return json.dumps({"drafts": drafts}, ensure_ascii=False)
