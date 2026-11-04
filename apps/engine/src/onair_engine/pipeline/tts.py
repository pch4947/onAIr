"""TTS 어댑터 — dummy(무음 wav), edge(청취 테스트), google(Cloud Text-to-Speech API),
gemini(운영 TTS — Gemini TTS), cartesia(비교 측정용 — Sonic REST API).

어댑터는 out_path에 오디오를 쓰기만 한다. 원자적 쓰기와 캐시는 audio.CachedTts가 감싼다.
제공자를 바꿀 때(유료 모델, 로컬 오픈소스 TTS) TtsClient 프로토콜만 지키면 된다.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Protocol

import mutagen

from .. import __version__

logger = logging.getLogger(__name__)

DEFAULT_EDGE_VOICE = "ko-KR-SunHiNeural"
DEFAULT_GOOGLE_VOICE = "ko-KR-Chirp3-HD-Aoede"


class TtsClient(Protocol):
    file_ext: str  # 어댑터마다 출력 포맷이 달라 파일 확장자를 어댑터가 정한다
    cache_namespace: str  # 같은 텍스트라도 제공자·보이스·설정이 다르면 다른 캐시 항목이다

    async def synthesize(self, text: str, out_path: Path) -> int:
        """out_path에 오디오 파일을 쓰고 duration_ms를 반환한다."""
        ...


def measure_duration_ms(path: Path) -> int | None:
    """실제 오디오 길이 — 버퍼 계산의 기준이므로 추정 대신 파일에서 잰다. 해석 불가면 None."""
    try:
        audio = mutagen.File(path)
    except mutagen.MutagenError:
        return None
    length = getattr(getattr(audio, "info", None), "length", 0)
    return round(length * 1000) if length else None


class DummyTtsClient:
    """텍스트 길이에 비례한 무음 wav — 관통 확인·테스트용."""

    file_ext = ".wav"
    cache_namespace = "dummy"
    RATE = 16000

    async def synthesize(self, text: str, out_path: Path) -> int:
        await asyncio.sleep(0.05)
        duration_ms = max(1000, min(8000, len(text) * 90))  # 대략의 한국어 발화 속도 흉내
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(b"\x00\x00" * (self.RATE * duration_ms // 1000))
        return duration_ms


class EdgeTtsClient:
    """Microsoft Edge 온라인 TTS — API 키 없이 한국어 뉴럴 보이스로 대본을 실제로 읽는다.

    청취 테스트용. 인터넷 연결이 필요하고 비공식 엔드포인트라 운영에는 쓰지 않는다.
    출력은 24kHz 48kbps mono mp3로 고정이다.
    """

    file_ext = ".mp3"
    _BYTES_PER_MS = 48_000 / 8 / 1000  # 48kbps CBR — 실측 실패 시 대체값

    def __init__(self, voice: str = DEFAULT_EDGE_VOICE):
        try:
            import edge_tts
        except ImportError as exc:  # 선택 의존성 — 방송 도중이 아니라 기동 시점에 실패시킨다
            raise RuntimeError('edge TTS를 쓰려면 pip install -e ".[tts]" 가 필요합니다') from exc
        self._edge_tts = edge_tts
        self.voice = voice
        self.cache_namespace = f"edge/{voice}"

    async def synthesize(self, text: str, out_path: Path) -> int:
        audio = bytearray()
        async for chunk in self._edge_tts.Communicate(text, self.voice).stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        return measure_duration_ms(out_path) or round(len(audio) / self._BYTES_PER_MS)


class GoogleTtsClient:
    """Google Cloud Text-to-Speech REST v1 — 기본 보이스는 Chirp 3 HD 한국어.

    인증은 API 키(GOOGLE_TTS_API_KEY 환경변수)를 X-Goog-Api-Key 헤더로 보낸다.
    HttpTransport와 같이 표준 라이브러리만 쓰고, 블로킹 urlopen은 to_thread로 감싼다.
    """

    file_ext = ".mp3"
    ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"

    def __init__(self, voice: str = DEFAULT_GOOGLE_VOICE, *, api_key: str | None = None,
                 speaking_rate: float = 1.0, endpoint: str = ENDPOINT, timeout: float = 15.0):
        api_key = api_key or os.environ.get("GOOGLE_TTS_API_KEY")
        if not api_key:  # 방송 도중이 아니라 기동 시점에 실패시킨다
            raise RuntimeError("google TTS를 쓰려면 GOOGLE_TTS_API_KEY가 필요합니다 (apps/engine/.env 또는 환경변수)")
        self._api_key = api_key
        self.voice = voice
        self.language_code = "-".join(voice.split("-")[:2])  # ko-KR-Chirp3-HD-Aoede -> ko-KR
        self.speaking_rate = speaking_rate
        self._endpoint = endpoint
        self._timeout = timeout
        self.cache_namespace = f"google/{voice}/rate{speaking_rate}"

    async def synthesize(self, text: str, out_path: Path) -> int:
        body = {
            "input": {"text": text},
            "voice": {"languageCode": self.language_code, "name": self.voice},
            "audioConfig": {"audioEncoding": "MP3", "speakingRate": self.speaking_rate},
        }
        audio = await asyncio.to_thread(self._request, body)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        duration_ms = measure_duration_ms(out_path)
        if duration_ms is None:
            raise RuntimeError(f"google TTS 응답을 오디오로 해석할 수 없습니다: {out_path}")
        return duration_ms

    def _request(self, body: dict) -> bytes:
        req = urllib.request.Request(
            self._endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("X-Goog-Api-Key", self._api_key)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # 400(보이스 이름 오류)·403(키 제한, API 미사용 설정)의 원인은 응답 본문에 있다
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"google TTS {exc.code}: {detail}") from exc
        return base64.b64decode(payload["audioContent"])


CARTESIA_API = "https://api.cartesia.ai"
CARTESIA_VERSION = "2026-08-14"  # Cartesia-Version 헤더 — 현재 API가 받는 유일한 값
DEFAULT_CARTESIA_MODEL = "sonic-3.6-2026-08-27"  # 날짜 고정 스냅샷 — 별칭(sonic-3.6)은 실험 중 바뀔 수 있다


def _cartesia_key(api_key: str | None) -> str:
    api_key = api_key or os.environ.get("CARTESIA_API_KEY")
    if not api_key:  # 방송 도중이 아니라 기동 시점에 실패시킨다
        raise RuntimeError("cartesia TTS를 쓰려면 CARTESIA_API_KEY가 필요합니다 (apps/engine/.env 또는 환경변수)")
    return api_key


def _cartesia_request(url: str, api_key: str, timeout: float, body: dict | None = None) -> bytes:
    """body가 있으면 POST(JSON), 없으면 GET. 응답 본문을 그대로 돌려준다."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Cartesia-Version", CARTESIA_VERSION)
    req.add_header("User-Agent", f"onair-engine/{__version__}")  # LLM 어댑터와 같은 이유 (llm._post_json)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        # 400(보이스 ID·언어 오류)·401(키)·429(한도)의 원인은 응답 본문에 있다
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"cartesia TTS {exc.code}: {detail}") from exc


class CartesiaTtsClient:
    """Cartesia Sonic REST (POST /tts/bytes) — 비교 측정용 (운영은 2026-10-30부터 Gemini).

    보이스는 Cartesia 보이스 ID(UUID)이고 기본값이 없다 — 방마다 호스트가 고르는 값이기 때문이다.
    한국어 보이스 목록은 list_cartesia_voices()(onair-engine --list-voices)로 본다.
    응답 본문이 곧 mp3라 Google처럼 base64를 풀 필요가 없다.
    """

    file_ext = ".mp3"

    def __init__(self, voice: str | None = None, *, api_key: str | None = None,
                 model: str = DEFAULT_CARTESIA_MODEL, language: str = "ko", speed: float = 1.0,
                 base_url: str = CARTESIA_API, timeout: float = 15.0):
        self._api_key = _cartesia_key(api_key)
        if not voice:
            raise RuntimeError("cartesia TTS는 보이스 ID가 필요합니다 (pipeline.tts_voice) "
                               "— 한국어 보이스 목록: onair-engine --list-voices")
        self.voice = voice
        self.model = model
        self.language = language
        self.speed = speed
        self._endpoint = f"{base_url.rstrip('/')}/tts/bytes"
        self._timeout = timeout
        self.cache_namespace = f"cartesia/{model}/{voice}/speed{speed}"

    async def synthesize(self, text: str, out_path: Path) -> int:
        body = {
            "model_id": self.model,
            "transcript": text,
            "voice": {"id": self.voice},
            "language": self.language,
            "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
            "generation_config": {"speed": self.speed},
        }
        audio = await asyncio.to_thread(_cartesia_request, self._endpoint, self._api_key,
                                        self._timeout, body)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        duration_ms = measure_duration_ms(out_path)
        if duration_ms is None:
            raise RuntimeError(f"cartesia TTS 응답을 오디오로 해석할 수 없습니다: {out_path}")
        return duration_ms


def _speaks_natively(voice: dict, language: str) -> bool:
    accents = voice.get("accents") or []
    if accents:
        return any(a.get("is_native") and str(a.get("locale", "")).lower().startswith(language)
                   for a in accents)
    return voice.get("language") == language  # accents가 없는 응답 — 폐기 예정 필드로 대신 판단


def list_cartesia_voices(language: str = "ko", *, api_key: str | None = None,
                         base_url: str = CARTESIA_API, timeout: float = 15.0) -> list[dict]:
    """해당 언어를 말하는 보이스 전체 — [{"id", "name", "gender", "description", "native"}].

    방 생성 폼의 보이스 목록과 persona voice 검증의 원천이다. 페이지를 끝까지 넘긴다.
    language 필터는 그 언어를 "말할 수 있는" 다국어 보이스(영어 원어민 등)까지 돌려준다.
    native는 그 언어가 원어민 억양인지다 — accents에 (ko-KR, is_native=True)가 있는 보이스.
    """
    api_key = _cartesia_key(api_key)
    voices: list[dict] = []
    cursor: str | None = None
    while True:
        query = {"language": language, "limit": "100"}
        if cursor:
            query["starting_after"] = cursor
        url = f"{base_url.rstrip('/')}/voices?{urllib.parse.urlencode(query)}"
        page = json.loads(_cartesia_request(url, api_key, timeout))
        voices += [{"id": v["id"], "name": v["name"], "gender": v.get("gender"),
                    "description": v.get("description", ""),
                    "native": _speaks_natively(v, language)} for v in page["data"]]
        cursor = page.get("next_page")
        if not page.get("has_more") or not cursor:
            return voices


GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_TTS_MODEL = "gemini-3.8-flash-tts"
DEFAULT_GEMINI_VOICE = "kore"  # 로컬 실행용 — 운영은 방마다 persona.voice로 정한다


def _gemini_key(api_key: str | None) -> str:
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:  # 방송 도중이 아니라 기동 시점에 실패시킨다
        raise RuntimeError("gemini TTS를 쓰려면 GEMINI_API_KEY가 필요합니다 (apps/engine/.env 또는 환경변수)")
    return api_key


class TtsQuotaError(RuntimeError):
    """제공자 한도 초과(429) — retry_after초 동안은 다시 호출해도 소용없다."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


_RETRY_IN = re.compile(r"retry in\s+((?:[\d.]+\s*[hms](?![a-z])\s*)+)", re.IGNORECASE)
_RETRY_PART = re.compile(r"([\d.]+)\s*([hms])", re.IGNORECASE)
_UNIT_SEC = {"h": 3600, "m": 60, "s": 1}


def _quota_wait_sec(retry_after: str | None, detail: str) -> float:
    """429 뒤 기다릴 시간. Retry-After 헤더 → 본문의 'retry in 23s' → 한도 단위별 기본값 순서로 본다."""
    try:
        if retry_after:
            return max(1.0, float(retry_after))
    except ValueError:  # 날짜 형식 헤더 — 본문으로 판단한다
        pass
    if match := _RETRY_IN.search(detail):
        total = sum(float(n) * _UNIT_SEC[unit.lower()] for n, unit in _RETRY_PART.findall(match[1]))
        return max(1.0, total)
    return 3600.0 if "per day" in detail else 60.0


def _gemini_request(url: str, api_key: str, timeout: float, body: dict | None = None) -> dict:
    """body가 있으면 POST(JSON), 없으면 GET. 응답 JSON을 돌려준다."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("x-goog-api-key", api_key)
    req.add_header("User-Agent", f"onair-engine/{__version__}")  # LLM 어댑터와 같은 이유 (llm._post_json)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # 400(보이스 ID 오류)·403(키)·429(한도)의 원인은 응답 본문에 있다
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 429:
            raise TtsQuotaError(f"gemini TTS 429: {detail}",
                                _quota_wait_sec(exc.headers.get("Retry-After"), detail)) from exc
        raise RuntimeError(f"gemini TTS {exc.code}: {detail}") from exc


def _gemini_audio(payload: dict) -> bytes:
    """응답에서 마지막 오디오 블록을 꺼낸다 — steps[type=model_output].content[type=audio].data."""
    blocks = [c for step in payload.get("steps", []) if step.get("type") == "model_output"
              for c in step.get("content", []) if c.get("type") == "audio"]
    if not blocks or not blocks[-1].get("data"):
        raise RuntimeError(f"gemini TTS 응답에 오디오가 없습니다: {str(payload)[:200]}")
    return base64.b64decode(blocks[-1]["data"])


class GeminiTtsClient:
    """Gemini TTS (POST /interactions) — 운영 TTS (2026-10-30 결정).

    보이스는 기본 보이스 ID(kore 등) 또는 voice design으로 만든 voice_... ID다. 언어는 입력에서
    자동 감지되므로 지정하지 않는다. 응답의 base64를 풀면 WAV(24kHz, mono, 16-bit)다.
    """

    file_ext = ".wav"

    def __init__(self, voice: str = DEFAULT_GEMINI_VOICE, *, api_key: str | None = None,
                 model: str = DEFAULT_GEMINI_TTS_MODEL, base_url: str = GEMINI_API,
                 timeout: float = 30.0):
        self._api_key = _gemini_key(api_key)
        self.voice = voice
        self.model = model
        self._endpoint = f"{base_url.rstrip('/')}/interactions"
        self._timeout = timeout
        self.cache_namespace = f"gemini/{model}/{voice}"
        self.blocked_until = 0.0  # 한도 초과로 호출하지 않는 시각까지 — 스케줄러도 이 값을 본다

    async def synthesize(self, text: str, out_path: Path) -> int:
        wait = self.blocked_until - time.time()
        if wait > 0:
            raise TtsQuotaError(f"gemini TTS 한도 초과로 {wait:.0f}초 동안 호출하지 않습니다", wait)
        body = {
            "model": self.model,
            "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": self.voice}]},
        }
        try:
            payload = await asyncio.to_thread(_gemini_request, self._endpoint, self._api_key,
                                              self._timeout, body)
        except TtsQuotaError as exc:
            self.blocked_until = time.time() + exc.retry_after
            logger.warning("TTS_QUOTA_BLOCKED %s %.0fs", self.model, exc.retry_after)
            raise
        audio = _gemini_audio(payload)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        duration_ms = measure_duration_ms(out_path)
        if duration_ms is None:
            raise RuntimeError(f"gemini TTS 응답을 오디오로 해석할 수 없습니다: {out_path}")
        return duration_ms


def _gemini_native(voice: dict, language: str) -> bool:
    code = voice.get("language_code") or ""
    codes = [code] if isinstance(code, str) else code
    return any(str(c).lower().startswith(language) for c in codes)


def list_gemini_voices(language: str = "ko", *, api_key: str | None = None,
                       base_url: str = GEMINI_API, timeout: float = 15.0) -> list[dict]:
    """보이스 전체 — [{"id", "name", "gender", "description", "native"}]. 페이지를 끝까지 넘긴다.

    Gemini는 입력 언어를 자동 감지하므로 다른 언어로 표시된 보이스도 한국어를 읽을 수 있다.
    그래서 서버에서 언어로 거르지 않고, persona voice 검증은 전체 목록으로 한다.
    native는 보이스의 language_code가 그 언어인지다 — 폼의 기본 선택지가 된다.
    """
    api_key = _gemini_key(api_key)
    voices: list[dict] = []
    token: str | None = None
    while True:
        query = {"page_size": "1000"}
        if token:
            query["page_token"] = token
        url = f"{base_url.rstrip('/')}/voices?{urllib.parse.urlencode(query)}"
        page = _gemini_request(url, api_key, timeout)
        voices += [{"id": v["id"], "name": v.get("display_name") or v["id"],
                    "gender": v.get("gender"), "description": v.get("description", ""),
                    "native": _gemini_native(v, language)} for v in page.get("voices", [])]
        token = page.get("next_page_token")
        if not token:
            return voices


# 방 생성 폼의 보이스 목록과 persona voice 검증을 제공하는 운영 제공자
VOICE_LISTERS = {"gemini": list_gemini_voices, "cartesia": list_cartesia_voices}


def make_tts(kind: str, *, voice: str | None = None) -> TtsClient:
    if kind == "dummy":
        return DummyTtsClient()
    if kind == "edge":
        return EdgeTtsClient(voice or DEFAULT_EDGE_VOICE)
    if kind == "google":
        return GoogleTtsClient(voice or DEFAULT_GOOGLE_VOICE)
    if kind == "gemini":
        return GeminiTtsClient(voice or DEFAULT_GEMINI_VOICE)
    if kind == "cartesia":
        return CartesiaTtsClient(voice)
    raise ValueError(f"unknown tts adapter: {kind}")
