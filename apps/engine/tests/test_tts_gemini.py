"""GeminiTtsClient — 로컬 HTTP 서버로 REST 요청 형식과 응답 처리를 확인한다 (실제 API 호출 없음)."""
import asyncio
import base64
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from onair_engine.pipeline.tts import GeminiTtsClient, list_gemini_voices, make_tts


def _wav_bytes(ms: int, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate * ms // 1000))
    return buf.getvalue()


def _audio_response(audio: bytes) -> bytes:
    return json.dumps({"steps": [
        {"type": "thought", "content": []},
        {"type": "model_output", "content": [
            {"type": "audio", "data": base64.b64encode(audio).decode()}]},
    ]}).encode()


def _serve(responses):
    """요청마다 responses에서 (status, body bytes)를 하나씩 꺼내 돌려주는 서버 — (base_url, 기록, 서버)."""
    received = []
    queue = list(responses)

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, body_json):
            received.append({"method": self.command, "path": self.path, "json": body_json,
                             "key": self.headers.get("x-goog-api-key")})
            status, body = queue.pop(0)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self._reply(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def do_GET(self):
            self._reply(None)

        def log_message(self, *args):  # 테스트 출력 오염 방지
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", received, server


def test_synthesize_sends_rest_request_and_writes_audio(tmp_path):
    audio = _wav_bytes(1500)
    base_url, received, server = _serve([(200, _audio_response(audio))])
    try:
        tts = GeminiTtsClient("kore", api_key="test-key", base_url=base_url)
        duration_ms = asyncio.run(tts.synthesize("안녕하세요, 새벽입니다.", tmp_path / "seg.wav"))
    finally:
        server.shutdown()

    assert duration_ms == 1500
    assert (tmp_path / "seg.wav").read_bytes() == audio
    req = received[0]
    assert (req["method"], req["path"], req["key"]) == ("POST", "/interactions", "test-key")
    assert req["json"] == {
        "model": "gemini-3.8-flash-tts",
        "input": [{"type": "user_input",
                   "content": [{"type": "text", "text": "안녕하세요, 새벽입니다."}]}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": "kore"}]},
    }


def test_cache_namespace_separates_voice_and_model():
    a = GeminiTtsClient("kore", api_key="k")
    b = GeminiTtsClient("puck", api_key="k")
    c = GeminiTtsClient("kore", api_key="k", model="gemini-3.8-flash-lite-tts")
    assert len({a.cache_namespace, b.cache_namespace, c.cache_namespace}) == 3


def test_api_error_is_raised_with_detail(tmp_path):
    base_url, _, server = _serve([(400, b'{"error": {"message": "voice not found"}}')])
    try:
        tts = GeminiTtsClient("Nope", api_key="test-key", base_url=base_url)
        with pytest.raises(RuntimeError, match="400.*voice not found"):
            asyncio.run(tts.synthesize("테스트", tmp_path / "seg.wav"))
    finally:
        server.shutdown()


def test_response_without_audio_is_rejected(tmp_path):
    base_url, _, server = _serve([(200, b'{"steps": [{"type": "model_output", "content": []}]}')])
    try:
        tts = GeminiTtsClient("kore", api_key="test-key", base_url=base_url)
        with pytest.raises(RuntimeError, match="오디오가 없습니다"):
            asyncio.run(tts.synthesize("테스트", tmp_path / "seg.wav"))
    finally:
        server.shutdown()


def test_undecodable_audio_is_rejected(tmp_path):
    base_url, _, server = _serve([(200, _audio_response(b"not audio"))])
    try:
        tts = GeminiTtsClient("kore", api_key="test-key", base_url=base_url)
        with pytest.raises(RuntimeError, match="오디오로 해석할 수 없습니다"):
            asyncio.run(tts.synthesize("테스트", tmp_path / "seg.wav"))
    finally:
        server.shutdown()


def test_missing_api_key_fails_at_startup(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        make_tts("gemini", voice="kore")


def test_make_tts_defaults_voice(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert make_tts("gemini").voice == "kore"


def test_list_voices_follows_pages():
    page1 = {"voices": [{"id": "voice_abc", "display_name": "새벽", "language_code": "ko-KR",
                         "gender": "female", "description": "차분함"}],
             "next_page_token": "p2"}
    page2 = {"voices": [{"id": "kore", "display_name": "Kore", "language_code": "en-US"}]}
    base_url, received, server = _serve([(200, json.dumps(page1).encode()),
                                         (200, json.dumps(page2).encode())])
    try:
        voices = list_gemini_voices(api_key="test-key", base_url=base_url)
    finally:
        server.shutdown()

    assert voices == [
        {"id": "voice_abc", "name": "새벽", "gender": "female", "description": "차분함", "native": True},
        {"id": "kore", "name": "Kore", "gender": None, "description": "", "native": False},
    ]
    first, second = (parse_qs(urlparse(r["path"]).query) for r in received)
    assert first == {"page_size": ["1000"]}
    assert second["page_token"] == ["p2"]
