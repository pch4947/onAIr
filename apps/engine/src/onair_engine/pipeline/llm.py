"""LLM 어댑터 — dummy(고정 대본), claude(Anthropic Messages API), gemini(Gemini API),
openai(Chat Completions 호환 — OpenAI, Qwen(DashScope), Ollama 등).

제공자는 아직 비교 실측 중이다(확인 3). 그래서 둘 다 같은 조건으로 붙여 두고 설정으로 고른다.
GoogleTtsClient와 같이 표준 라이브러리 REST만 쓰고, 블로킹 urlopen은 to_thread로 감싼다.
어댑터는 대본을 방송용으로 다듬어(clean_script) 돌려준다. 부적합 판단은 Script("REJECT")다.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import urllib.error
import urllib.request
from typing import Protocol

from .. import __version__
from ..domain import Prompt, Script

DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5"  # 짧은 멘트는 지연이 우선이다. 품질 비교는 llm_model로
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
REJECT = "REJECT"


class LlmClient(Protocol):
    async def generate(self, prompt: Prompt) -> Script: ...


# 더미 대본 — TTS로 실제로 읽혀도 방송처럼 들리도록 쓴 코너별 고정 문장.
# 슬롯({dj}, {title} 등)은 프롬프트에서 추출해 채운다 (_slots 참고).
# 슬롯 값의 받침 유무를 모르므로 슬롯 바로 뒤에는 조사(은/는, 이/가, 예요/이에요)를 붙이지 않는다.
_TEMPLATES: dict[str, list[str]] = {
    "opening": [
        ("안녕하세요, {concept}의 {dj}입니다. 오늘도 이 시간을 함께해 주셔서 고마워요. "
         "편하게 들으면서 하던 일 이어가세요."),
        "{concept}, 지금 시작합니다. DJ {dj}입니다. 잠깐 숨 한 번 고르고, 천천히 같이 가봐요.",
        "반가워요, {dj}입니다. 오늘 하루는 어떠셨나요? 지금부터 조용히 곁에 있을게요.",
    ],
    "briefing": [
        "잠깐 소식 하나 전해드릴게요. {headline}. 자세한 내용은 원문 출처에서 확인해 보세요.",
        "요즘 이런 이야기가 있더라고요. {headline}. 다음 곡 듣기 전에 한 번쯤 생각해 볼 만하죠.",
    ],
    "music_intro": [
        "이번 곡은 {artist}의 {title}입니다. {mood} 분위기라 지금 시간에 잘 어울릴 거예요.",
        "잠깐 쉬어가는 의미로 한 곡 준비했어요. {artist}, {title}. 이어서 들려드릴게요.",
        "{mood} 곡 하나 골라봤어요. {artist}의 {title}, 함께 들어요.",
    ],
    "request_reply": [
        ("사연 하나 읽어드릴게요. {story}. 이렇게 보내주셨어요. 그런 날도 있죠. "
         "이 시간만큼은 너무 애쓰지 말고 편하게 쉬어가셨으면 좋겠어요."),
        ("보내주신 이야기 잘 받았어요. {story}. 마음이 전해져요. "
         "지금 이 방송을 듣는 모두가 함께라는 걸 기억해 주세요."),
    ],
    "filler": [
        "지금 이 순간도 충분히 잘 해내고 계신 거예요. 곧 다음 곡 이어갈게요.",
        "잠깐 기지개 한 번 켜볼까요? 어깨 힘 빼고, 음악은 계속됩니다.",
        "물 한 모금 마시고 오세요. 저 {dj}, 여기 그대로 있을게요.",
    ],
}

# 실 LLM처럼 프롬프트만 받으므로 코너 종류는 코너 프롬프트 문구로 판별한다.
# corners/*.py의 프롬프트 문구를 바꾸면 여기도 맞춰야 한다 (test_smoke가 어긋남을 검출).
_CORNER_MARKERS = [
    ("오프닝", "opening"),
    ("브리핑", "briefing"),
    ("곡을 소개", "music_intro"),
    ("사연:", "request_reply"),
    ("브릿지", "filler"),
]

_PROFILE_RE = re.compile(r"\[(?P<concept>.+?)\]의 DJ (?P<dj>.+?)다\.")
_TRACK_RE = re.compile(r"곡: (?P<title>.+?) — (?P<artist>.+?) \(무드: (?P<mood>.+?)\)")
_STORY_RE = re.compile(r"사연: (?P<story>.+)", re.DOTALL)
_HEADLINE_RE = re.compile(r"^- (?P<headline>.+?): ", re.MULTILINE)

_MOOD_KO = {"calm": "차분한", "mellow": "포근한", "hopeful": "희망찬"}


def detect_corner(user_prompt: str) -> str | None:
    return next((corner for marker, corner in _CORNER_MARKERS if marker in user_prompt), None)


def _slots(prompt: Prompt) -> dict[str, str]:
    slots = {
        "dj": "DJ", "concept": "우리 방송",
        "title": "다음 곡", "artist": "오늘의 아티스트", "mood": "잔잔한",
        "story": "오늘 하루 이야기", "headline": "오늘의 소식",
    }
    for regex, text in [(_PROFILE_RE, prompt.system), (_TRACK_RE, prompt.user),
                        (_STORY_RE, prompt.user), (_HEADLINE_RE, prompt.user)]:
        if m := regex.search(text):
            slots.update({k: v.strip() for k, v in m.groupdict().items()})
    slots["mood"] = _MOOD_KO.get(slots["mood"], slots["mood"])
    slots["story"] = slots["story"][:60]
    return slots


class DummyLlmClient:
    """코너별 고정 대본 반환 — API 키 없이 파이프라인 관통·청취 테스트·발표장 폴백용."""

    def __init__(self, delay_sec: float = 0.2, seed: int | None = None):
        self.delay_sec = delay_sec
        self._rng = random.Random(seed)

    async def generate(self, prompt: Prompt) -> Script:
        await asyncio.sleep(self.delay_sec)  # 생성 지연 흉내
        corner = detect_corner(prompt.user) or "filler"
        template = self._rng.choice(_TEMPLATES[corner])
        return Script(text=template.format_map(_slots(prompt)))


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
        raise RuntimeError(f"{kind} LLM을 쓰려면 {env} 환경변수가 필요합니다")
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
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
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
        if data.get("stop_reason") == "refusal":  # 모델 자체 거부 = L1 REJECT와 같게 다룬다
            return Script(text=REJECT)
        return Script(text=clean_script(text, truncated=data.get("stop_reason") == "max_tokens"))


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
        config: dict = {"maxOutputTokens": self.max_tokens, "temperature": self.temperature}
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
            return Script(text=REJECT)
        try:
            cand = data["candidates"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"gemini 응답 형식 오류: {str(data)[:200]}") from exc
        reason = cand.get("finishReason")
        if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            return Script(text=REJECT)
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return Script(text=clean_script(text, truncated=reason == "MAX_TOKENS"))


class OpenAiLlmClient:
    """Chat Completions 호환 API. base_url만 바꾸면 OpenAI 외 호환 서버에도 붙는다.

    - OpenAI: 기본 base_url, OPENAI_API_KEY 필수
    - Qwen(DashScope): https://dashscope-intl.aliyuncs.com/compatible-mode/v1, 키는 OPENAI_API_KEY로
    - Ollama: http://localhost:11434/v1, 키 불필요
    모델 ID는 기본값을 두지 않는다 — 제공자마다 다르고 자주 바뀐다.

    추론 모델은 temperature 변경을 거부하고 출력 한도를 추론 토큰으로 쓴다. 그래서
    temperature는 지정했을 때만 보내고, 지연을 줄이려면 reasoning_effort를 낮춘다.
    """

    def __init__(self, model: str | None, *, base_url: str = DEFAULT_OPENAI_BASE_URL,
                 api_key: str | None = None, max_tokens: int = 400,
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
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": prompt.system},
                         {"role": "user", "content": prompt.user}],
        }
        # OpenAI 본가는 max_tokens를 폐기했고, 호환 서버들은 아직 max_tokens만 아는 경우가 많다
        body["max_completion_tokens" if self._official else "max_tokens"] = self.max_tokens
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
            return Script(text=REJECT)  # 제공자 필터·모델 거부 = L1 REJECT와 같게 다룬다
        text = message.get("content") or ""
        if reason == "length" and not text.strip():
            raise LlmError("출력 한도를 추론 토큰이 다 썼습니다 — "
                           "reasoning_effort를 낮추거나 max_tokens를 늘리세요")
        return Script(text=clean_script(text, truncated=reason == "length"))


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
