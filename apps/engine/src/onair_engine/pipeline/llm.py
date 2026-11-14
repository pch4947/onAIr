"""LLM 어댑터 — dummy(고정 대본), claude(Anthropic Messages API), gemini(Gemini API),
openai(Chat Completions 호환 — OpenAI, Qwen(DashScope), Ollama 등).

제공자는 아직 비교 실측 중이다(확인 3). 그래서 둘 다 같은 조건으로 붙여 두고 설정으로 고른다.
GoogleTtsClient와 같이 표준 라이브러리 REST만 쓰고, 블로킹 urlopen은 to_thread로 감싼다.
어댑터는 대본을 방송용으로 다듬어(clean_script) 돌려준다. 부적합 판단은 Script("REJECT")다.
대본이 아닌 출력(persona 초안 JSON 등)은 complete()로 원문 그대로 받는다.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .. import __version__
from ..catalog import mood_ko
from ..corners.steps import BLOCK_MARKER, BLOCK_STEP, STEP_KINDS
from ..domain import Prompt, Script
from ..prompt_data import CHAT, STORY, TOPIC

DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5"  # 짧은 멘트는 지연이 우선이다. 품질 비교는 llm_model로
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
REJECT = "REJECT"


class LlmClient(Protocol):
    async def generate(self, prompt: Prompt) -> Script:
        """방송 대본 — clean_script를 거친다. 부적합 판단은 Script(REJECT)."""
        ...

    async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
        """원문 그대로 — JSON 등 구조화 출력용. 거부·잘림은 LlmError로 올린다."""
        ...


@dataclass
class _Reply:
    text: str
    truncated: bool  # 출력 한도에서 잘림
    refused: bool  # 제공자 안전 필터·모델 거부


def _raw(reply: _Reply, provider: str) -> str:
    """complete()의 공통 후처리 — 구조화 출력은 잘리거나 거부되면 쓸 수 없다."""
    if reply.refused:
        raise LlmError(f"{provider}가 요청을 거부했습니다")
    if reply.truncated:
        raise LlmError(f"{provider} 출력이 한도에서 잘렸습니다 — max_tokens를 늘리세요")
    return reply.text


# 더미 대본 — TTS로 실제로 읽혀도 방송처럼 들리도록 쓴 단계별 고정 문장 (단계 유형: corners/steps.py).
# 슬롯({dj}, {title} 등)은 프롬프트에서 추출해 채운다 (_slots 참고).
# 슬롯 값의 받침 유무를 모르므로 슬롯 바로 뒤에는 조사(은/는, 이/가, 예요/이에요)를 붙이지 않는다.
_TEMPLATES: dict[str, list[str]] = {
    "opening_greeting": [
        ("안녕하세요, {concept}의 {dj}입니다. 오늘도 이 시간을 함께해 주셔서 고마워요. "
         "편하게 들으면서 하던 일 이어가세요."),
        "{concept}, 지금 시작합니다. DJ {dj}입니다. 잠깐 숨 한 번 고르고, 천천히 같이 가봐요.",
        "반가워요, {dj}입니다. 오늘 하루는 어떠셨나요? 지금부터 조용히 곁에 있을게요.",
    ],
    "topic_intro": [
        "오늘 함께 이야기할 주제는 이거예요. {topic}. 편하게 생각 나누면서 들어 주세요.",
    ],
    "corner_preview": [
        "오늘 순서 잠깐 알려 드릴게요. 이야기와 음악이 차례로 이어집니다. 끝까지 함께해 주세요.",
    ],
    "talk_open": [
        "오늘 이야기 하나 꺼내 볼게요. {topic}. 여러분은 어떠세요?",
    ],
    "talk_resume": [
        "{topic}, 이번엔 조금 다른 쪽에서 생각해 볼게요. 여러분은 어떠세요?",
    ],
    "talk": [
        "이 이야기 조금 더 해 볼게요. 생각해 보면 정답이 있는 문제는 아닌 것 같아요.",
        "저는 요즘 이런 생각을 자주 해요. 너무 잘하려고 애쓰지 않아도 괜찮다는 거요.",
    ],
    # 덩어리(talk_block)의 문단 — 요청한 문단 수만큼 차례로 쓴다 (DummyLlmClient.complete)
    "talk_block": [
        "오늘 이야기 조금 더 이어가 볼게요. {topic}, 생각할수록 할 말이 많은 주제예요.",
        "저는 이런 얘기를 할 때 서두르지 않는 편이 좋더라고요. 천천히 떠올려 보면 장면이 하나씩 나오거든요.",
        "누구에게나 비슷한 기억이 하나쯤은 있을 거예요. 그게 꼭 대단한 일이 아니어도 괜찮고요.",
        "그래서 오늘은 정답을 찾기보다 그냥 같이 떠올려 보려고 해요. 그걸로 충분한 밤이니까요.",
        "이야기는 여기서 잠깐 쉬어 갈게요. 음악 듣는 동안에도 이 생각은 이어 가 보세요.",
    ],
    "artist_talk": [
        "다음 곡은 {artist}의 노래예요. 목소리만 들어도 분위기가 확 달라지는 아티스트죠.",
    ],
    "music_intro": [
        "이번 곡은 {artist}의 {title}입니다. {mood} 분위기라 지금 시간에 잘 어울릴 거예요.",
        "잠깐 쉬어가는 의미로 한 곡 준비했어요. {artist}, {title}. 이어서 들려드릴게요.",
        "{mood} 곡 하나 골라봤어요. {artist}의 {title}, 함께 들어요.",
    ],
    "story": [
        ("사연 하나 읽어드릴게요. {story}. 이렇게 보내주셨어요. 그런 날도 있죠. "
         "이 시간만큼은 너무 애쓰지 말고 편하게 쉬어가셨으면 좋겠어요."),
    ],
    "text_read": [
        "문자 하나 볼게요. {story}. 고마워요, 잘 읽었어요.",
    ],
    "game_ask": [
        "자, 이번 고민이에요. 둘 중에 하나만 골라 주세요. 화면에서 투표할 수 있어요, 여러분의 선택을 보여 주세요.",
    ],
    "game_talk": [
        "이쪽을 고르는 마음도 알 것 같아요. 아직 고르지 못하셨다면 지금 투표해 주세요.",
        "저쪽도 만만치 않죠. 여러분은 어느 쪽인가요, 투표는 아직 열려 있어요.",
    ],
    "game_reveal": [
        "이제 결과를 볼게요. 제 선택은 이쪽이었어요. 여러분의 표도 확인했습니다. 다음 고민으로 가 볼게요.",
    ],
    "game_final": [
        "오늘 승부는 여기까지예요. 함께 골라 주셔서 고마워요.",
    ],
    "news_headline": [
        "오늘의 주요 소식입니다. {headline}. 자세한 내용 이어서 전해 드리겠습니다.",
    ],
    "news_item": [
        "소식 전해 드립니다. {headline}. 자세한 내용은 원문 출처에서 확인하실 수 있습니다.",
    ],
    "news_recap": [
        "지금까지 전해 드린 소식 정리해 드렸습니다. 잠시 음악 듣고 다시 찾아오겠습니다.",
    ],
    "closing_lookback": [
        "오늘 방송도 어느새 마무리할 시간이네요. 함께해 주셔서 정말 고마웠어요.",
    ],
    "closing_farewell": [
        "마지막 곡 들려드리면서 인사할게요. {artist}의 {title}. 지금까지 {dj}였습니다.",
    ],
    "filler": [
        "지금 이 순간도 충분히 잘 해내고 계신 거예요. 곧 다음 곡 이어갈게요.",
        "잠깐 기지개 한 번 켜볼까요? 어깨 힘 빼고, 음악은 계속됩니다.",
        "물 한 모금 마시고 오세요. 저 {dj}, 여기 그대로 있을게요.",
    ],
}
# 인용할 채팅이 있으면 앞에 붙인다 — 파이프라인 인용 검사를 통과해야 한다
_QUOTE_TEMPLATE = "{quote}, 이렇게 보내 주신 분이 있네요. 고마워요. "

_PROFILE_RE = re.compile(r"\[(?P<concept>.+?)\]의 DJ (?P<dj>.+?)다\.")
_TRACK_RE = re.compile(r"곡: (?P<title>.+?) — (?P<artist>.+?)(?: \(무드: (?P<mood>.+?)\))?$",
                       re.MULTILINE)
_HEADLINE_RE = re.compile(r"^- (?P<headline>.+?): ", re.MULTILINE)
_DATA_RE = re.compile(r'<data label="[^"]*">\n.*?\n</data>', re.DOTALL)
_PARTS_RE = re.compile(r"문단 (\d+)개")


def _block(label: str) -> re.Pattern:
    return re.compile(rf'<data label="{label}">\n(?P<body>.*?)\n</data>', re.DOTALL)


_STORY_RE, _TOPIC_RE, _CHAT_RE = _block(STORY), _block(TOPIC), _block(CHAT)


def detect_step(user_prompt: str) -> str | None:
    """프롬프트의 단계 유형 — 지시문 속 marker 구절로 알아본다 (corners/steps.py STEP_KINDS).

    데이터 블록은 빼고 지시문만 본다 — "오프닝 멘트 해줘" 같은 사연으로 단계를 오판하지 않게.
    """
    instructions = _DATA_RE.sub("", user_prompt)
    return next((name for name, kind in STEP_KINDS.items() if kind.marker in instructions), None)


def _slots(prompt: Prompt) -> dict[str, str]:
    slots = {
        "dj": "DJ", "concept": "우리 방송",
        "title": "다음 곡", "artist": "오늘의 아티스트", "mood": "잔잔한",
        "story": "오늘 하루 이야기", "headline": "오늘의 소식", "topic": "오늘의 이야기",
    }
    for regex, text in [(_PROFILE_RE, prompt.system), (_TRACK_RE, prompt.user),
                        (_HEADLINE_RE, prompt.user)]:
        if m := regex.search(text):
            slots.update({k: v.strip() for k, v in m.groupdict().items() if v})
    if m := _STORY_RE.search(prompt.user):
        # 사연·문자 블록은 "닉네임: x" 줄로 시작한다 — 본문만 읽는다
        body = m["body"].split("\n", 1)[-1] if m["body"].startswith("닉네임:") else m["body"]
        slots["story"] = body.strip()
    if m := _TOPIC_RE.search(prompt.user):
        slots["topic"] = m["body"].strip()
    slots["mood"] = mood_ko(slots["mood"])
    slots["story"] = slots["story"][:60]
    return slots


class DummyLlmClient:
    """단계별 고정 대본 반환 — API 키 없이 파이프라인 관통·청취 테스트·발표장 폴백용."""

    def __init__(self, delay_sec: float = 0.2, seed: int | None = None):
        # 숫자, 또는 호출마다 지연(초)을 돌려주는 함수 — 시나리오 실행기가 실측 분포를 넣는다 (scenario/sim.py)
        self.delay_sec = delay_sec
        # 채팅 인용을 빠뜨릴 확률 — 실제 LLM의 인용 누락 흉내. 기본은 항상 인용한다
        self.quote_miss_rate = 0.0
        self._rng = random.Random(seed)

    async def _wait(self) -> None:
        await asyncio.sleep(self.delay_sec() if callable(self.delay_sec) else self.delay_sec)

    async def generate(self, prompt: Prompt) -> Script:
        await self._wait()  # 생성 지연 흉내
        step = detect_step(prompt.user) or "filler"
        text = self._rng.choice(_TEMPLATES[step]).format_map(_slots(prompt))
        m = _CHAT_RE.search(prompt.user)
        # 채팅은 한 줄에 한 건이다 — 건마다 인용하고, 빠뜨릴지도 건마다 정한다 (한 멘트에 여러 건, #267)
        for line in reversed(m["body"].splitlines() if m else []):
            if line.strip() and (not self.quote_miss_rate or self._rng.random() >= self.quote_miss_rate):
                text = _QUOTE_TEMPLATE.format(quote=line.strip()[:60]) + text
        return Script(text=text)

    async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
        await self._wait()
        from ..inputs import dilemmas, story_judge, topic_plan
        if BLOCK_MARKER in _DATA_RE.sub("", prompt.user):
            m = _PARTS_RE.search(prompt.user)
            lines = _TEMPLATES[BLOCK_STEP][:int(m.group(1)) if m else 3]
            return "\n\n".join(line.format_map(_slots(prompt)) for line in lines)
        if story_judge.MARKER in prompt.user:
            return story_judge.dummy_judgement_json(prompt)
        if topic_plan.MARKER in prompt.user:
            return topic_plan.dummy_plan_json(prompt)
        if dilemmas.MARKER in prompt.user:
            return dilemmas.dummy_dilemmas_json(prompt)
        # 나머지 구조화 출력은 persona 초안이다 — API 키 없이 방 생성 화면을 개발할 수 있게 한다
        from ..personas import dummy_drafts_json  # 순환 import 회피 (personas가 llm을 쓴다)
        return dummy_drafts_json(prompt)


class LlmError(RuntimeError):
    """호출 실패·응답 해석 불가 — 파이프라인이 재시도 후 filler로 대체한다."""


# TTS가 그대로 읽으면 방송 사고가 되는 것들 — 프롬프트로 막고, 새어 나온 것은 여기서 지운다
_MARKUP_RE = re.compile(r"[*_#`>]+")
_STAGE_RE = re.compile(r"\[[^\]]*\]|\([^)]*(?:음악|BGM|웃음|효과음|잠시|멈춤)[^)]*\)")
_SPEAKER_RE = re.compile(r"^\s*(?:DJ\s*[^\s:：]{0,10}|진행자|앵커)\s*[:：]\s*", re.MULTILINE)
_QUOTES = "\"'“”‘’「」『』"
_SENTENCE_END_RE = re.compile(r"[.!?…~]|[요다죠까네]\s")


def clean_script(text: str, *, truncated: bool = False) -> str:
    """LLM 출력을 TTS에 넣을 한 덩어리 대본으로 다듬는다.

    truncated(출력 토큰 한도에서 잘림)면 마지막 완결 문장까지만 남긴다 — 말이 끊기면 안 된다.
    """
    text = _STAGE_RE.sub(" ", text)
    text = _MARKUP_RE.sub("", text)  # 화자 표기가 **DJ 새벽:**처럼 감싸여 올 수 있어 먼저 벗긴다
    text = _SPEAKER_RE.sub("", text)
    text = " ".join(text.split()).strip(_QUOTES + " ")
    if text.rstrip(".!").upper() == REJECT:
        return REJECT
    if truncated:
        ends = list(_SENTENCE_END_RE.finditer(text + " "))
        if not ends:
            raise LlmError(f"출력이 한도에서 잘렸고 완결 문장이 없습니다: {text[:60]!r}")
        text = text[:ends[-1].end()].strip()
    if not text:
        raise LlmError("빈 대본")
    return text


def _post_json(url: str, body: dict, headers: dict[str, str], timeout: float,
               provider: str) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
    )
    req.add_header("Content-Type", "application/json; charset=utf-8")
    # urllib 기본값(Python-urllib/3.x)은 Cloudflare를 앞에 둔 API가 403 "error code: 1010"으로 막는다
    req.add_header("User-Agent", f"onair-engine/{__version__}")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # 400(모델 이름 오류)·401/403(키)·429(한도)의 원인은 응답 본문에 있다
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LlmError(f"{provider} {exc.code}: {detail}") from exc
    except (OSError, ValueError) as exc:  # 연결 실패·타임아웃·JSON 아님
        raise LlmError(f"{provider} 호출 실패: {exc!r}") from exc


def _require_key(api_key: str | None, env: str, kind: str) -> str:
    api_key = api_key or os.environ.get(env)
    if not api_key:  # 방송 도중이 아니라 기동 시점에 실패시킨다
        raise RuntimeError(f"{kind} LLM을 쓰려면 {env}가 필요합니다 (apps/engine/.env 또는 환경변수)")
    return api_key


class ClaudeLlmClient:
    """Anthropic Messages API. 인증은 ANTHROPIC_API_KEY를 x-api-key 헤더로 보낸다."""

    ENDPOINT = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL, *, api_key: str | None = None,
                 max_tokens: int = 400, temperature: float = 0.8, endpoint: str = ENDPOINT,
                 timeout: float = 20.0):
        self._api_key = _require_key(api_key, "ANTHROPIC_API_KEY", "claude")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._endpoint = endpoint
        self._timeout = timeout

    async def generate(self, prompt: Prompt) -> Script:
        reply = await self._call(prompt, self.max_tokens)
        if reply.refused:  # 모델 자체 거부 = L1 REJECT와 같게 다룬다
            return Script(text=REJECT)
        return Script(text=clean_script(reply.text, truncated=reply.truncated))

    async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
        return _raw(await self._call(prompt, max_tokens), "claude")

    async def _call(self, prompt: Prompt, max_tokens: int) -> _Reply:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "system": prompt.system,
            "messages": [{"role": "user", "content": prompt.user}],
        }
        headers = {"x-api-key": self._api_key, "anthropic-version": self.API_VERSION}
        data = await asyncio.to_thread(
            _post_json, self._endpoint, body, headers, self._timeout, "claude")
        try:
            text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise LlmError(f"claude 응답 형식 오류: {str(data)[:200]}") from exc
        stop = data.get("stop_reason")
        return _Reply(text, truncated=stop == "max_tokens", refused=stop == "refusal")


class GeminiLlmClient:
    """Gemini API generateContent. 인증은 GEMINI_API_KEY를 x-goog-api-key 헤더로 보낸다.

    2.5 계열은 기본으로 thinking을 켜서 지연과 출력 토큰을 쓴다. 짧은 멘트에는 필요 없어 끈다.
    """

    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, model: str = DEFAULT_GEMINI_MODEL, *, api_key: str | None = None,
                 max_tokens: int = 400, temperature: float = 0.8, endpoint: str = ENDPOINT,
                 timeout: float = 20.0, thinking_budget: int | None = 0):
        self._api_key = _require_key(api_key, "GEMINI_API_KEY", "gemini")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.thinking_budget = thinking_budget
        self._endpoint = endpoint.format(model=model)
        self._timeout = timeout

    async def generate(self, prompt: Prompt) -> Script:
        reply = await self._call(prompt, self.max_tokens)
        if reply.refused:
            return Script(text=REJECT)
        return Script(text=clean_script(reply.text, truncated=reply.truncated))

    async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
        return _raw(await self._call(prompt, max_tokens), "gemini")

    async def _call(self, prompt: Prompt, max_tokens: int) -> _Reply:
        config: dict = {"maxOutputTokens": max_tokens, "temperature": self.temperature}
        if self.thinking_budget is not None:
            config["thinkingConfig"] = {"thinkingBudget": self.thinking_budget}
        body = {
            "systemInstruction": {"parts": [{"text": prompt.system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt.user}]}],
            "generationConfig": config,
        }
        data = await asyncio.to_thread(
            _post_json, self._endpoint, body, {"x-goog-api-key": self._api_key},
            self._timeout, "gemini")
        # 입력이 제공자 안전 필터에 막히면 후보가 없다 — L1 REJECT와 같게 다룬다
        if (data.get("promptFeedback") or {}).get("blockReason"):
            return _Reply("", truncated=False, refused=True)
        try:
            cand = data["candidates"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"gemini 응답 형식 오류: {str(data)[:200]}") from exc
        reason = cand.get("finishReason")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return _Reply(text, truncated=reason == "MAX_TOKENS",
                      refused=reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"))


OPENAI_MAX_TOKENS = 1000


class OpenAiLlmClient:
    """Chat Completions 호환 API. base_url만 바꾸면 OpenAI 외 호환 서버에도 붙는다.

    - OpenAI: 기본 base_url, OPENAI_API_KEY 필수
    - Qwen(DashScope): https://dashscope-intl.aliyuncs.com/compatible-mode/v1, 키는 OPENAI_API_KEY로
    - Ollama: http://localhost:11434/v1, 키 불필요
    모델 ID는 기본값을 두지 않는다 — 제공자마다 다르고 자주 바뀐다.

    추론 모델은 temperature 변경을 거부하고 출력 한도를 추론 토큰으로 쓴다. 그래서
    temperature는 지정했을 때만 보내고, 지연을 줄이려면 reasoning_effort를 낮춘다.
    출력 한도는 대본과 추론을 합친 것이라 다른 제공자보다 넉넉히 둔다. 400일 때는 채팅을 인용하는
    멘트에서 추론이 한도를 다 써 빈 응답이 나왔다 — 한도를 풀고 재면 추론 466, 합계 564 토큰까지
    쓴다 (ENGINE_TUNING_LOG T-010). 대본 길이는 프롬프트의 문장 수 지시가 정하고, 이 한도는
    멈추지 않는 출력을 끊는 안전장치다.
    """

    def __init__(self, model: str | None, *, base_url: str = DEFAULT_OPENAI_BASE_URL,
                 api_key: str | None = None, max_tokens: int = OPENAI_MAX_TOKENS,
                 temperature: float | None = None, reasoning_effort: str | None = None,
                 timeout: float = 20.0):
        if not model:  # 방송 도중이 아니라 기동 시점에 실패시킨다
            raise RuntimeError("openai LLM은 모델 ID가 필요합니다 (--llm-model 또는 pipeline.llm_model)")
        self._official = base_url.rstrip("/") == DEFAULT_OPENAI_BASE_URL
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if self._official:
            api_key = _require_key(api_key, "OPENAI_API_KEY", "openai")
        self._api_key = api_key  # 호환 서버(Ollama 등)는 키 없이도 된다
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout

    async def generate(self, prompt: Prompt) -> Script:
        reply = await self._call(prompt, self.max_tokens)
        if reply.refused:  # 제공자 필터·모델 거부 = L1 REJECT와 같게 다룬다
            return Script(text=REJECT)
        return Script(text=clean_script(reply.text, truncated=reply.truncated))

    async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
        return _raw(await self._call(prompt, max_tokens), "openai")

    async def _call(self, prompt: Prompt, max_tokens: int) -> _Reply:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": prompt.system},
                         {"role": "user", "content": prompt.user}],
        }
        # OpenAI 본가는 max_tokens를 폐기했고, 호환 서버들은 아직 max_tokens만 아는 경우가 많다
        body["max_completion_tokens" if self._official else "max_tokens"] = max_tokens
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = await asyncio.to_thread(
            _post_json, self._endpoint, body, headers, self._timeout, "openai")
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"openai 응답 형식 오류: {str(data)[:200]}") from exc
        reason = choice.get("finish_reason")
        if reason == "content_filter" or message.get("refusal"):
            return _Reply("", truncated=False, refused=True)
        text = message.get("content") or ""
        if reason == "length" and not text.strip():
            raise LlmError("출력 한도를 추론 토큰이 다 썼습니다 — "
                           "reasoning_effort를 낮추거나 max_tokens를 늘리세요")
        return _Reply(text, truncated=reason == "length", refused=False)


def make_llm(kind: str, *, model: str | None = None, base_url: str | None = None,
             reasoning_effort: str | None = None) -> LlmClient:
    if kind == "dummy":
        return DummyLlmClient()
    if kind == "claude":
        return ClaudeLlmClient(model or DEFAULT_CLAUDE_MODEL)
    if kind == "gemini":
        return GeminiLlmClient(model or DEFAULT_GEMINI_MODEL)
    if kind == "openai":
        return OpenAiLlmClient(model, base_url=base_url or DEFAULT_OPENAI_BASE_URL,
                               reasoning_effort=reasoning_effort)
    raise ValueError(f"unknown llm adapter: {kind}")
