"""CartesiaTtsClient — 로컬 HTTP 서버로 REST 요청 형식과 응답 처리를 확인한다 (실제 API 호출 없음)."""
import asyncio
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from onair_engine.pipeline.tts import (
    CARTESIA_VERSION,
    CartesiaTtsClient,
    list_cartesia_voices,
    make_tts,
)

VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"


def _wav_bytes(ms: int, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate * ms // 1000))
    return buf.getvalue()


def _serve(responses):
    """요청마다 responses에서 (status, body bytes)를 하나씩 꺼내 돌려주는 서버 — (base_url, 기록, 서버)."""
    received = []
    queue = list(responses)

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, body_json):
            received.append({
                "method": self.command, "path": self.path, "json": body_json,
                "auth": self.headers.get("Authorization"),
                "version": self.headers.get("Cartesia-Version"),
            })
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
    base_url, received, server = _serve([(200, audio)])
    try:
        tts = CartesiaTtsClient(VOICE, api_key="test-key", base_url=base_url)
        duration_ms = asyncio.run(tts.synthesize("안녕하세요, 새벽입니다.", tmp_path / "seg.audio"))
    finally:
        server.shutdown()

    assert duration_ms == 1500
    assert (tmp_path / "seg.audio").read_bytes() == audio
    req = received[0]
    assert (req["method"], req["path"]) == ("POST", "/tts/bytes")
    assert req["auth"] == "Bearer test-key"
    assert req["version"] == CARTESIA_VERSION
    assert req["json"] == {
        "model_id": "sonic-3.6-2026-08-27",
        "transcript": "안녕하세요, 새벽입니다.",
        "voice": {"id": VOICE},
        "language": "ko",
        "output_format": {"container": "mp3", "sample_rate": 44100, "bit_rate": 128000},
        "generation_config": {"speed": 1.0},
    }


def test_cache_namespace_separates_voice_and_model():
    a = CartesiaTtsClient(VOICE, api_key="k")
    b = CartesiaTtsClient("other-voice", api_key="k")
    c = CartesiaTtsClient(VOICE, api_key="k", model="sonic-3.5")
    assert len({a.cache_namespace, b.cache_namespace, c.cache_namespace}) == 3


def test_api_error_is_raised_with_detail(tmp_path):
    base_url, _, server = _serve([(400, b'{"error": "voice not found"}')])
    try:
        tts = CartesiaTtsClient(VOICE, api_key="test-key", base_url=base_url)
        with pytest.raises(RuntimeError, match="400.*voice not found"):
            asyncio.run(tts.synthesize("테스트", tmp_path / "seg.audio"))
    finally:
        server.shutdown()


def test_non_audio_response_is_rejected(tmp_path):
    base_url, _, server = _serve([(200, b"not audio")])
    try:
        tts = CartesiaTtsClient(VOICE, api_key="test-key", base_url=base_url)
        with pytest.raises(RuntimeError, match="오디오로 해석할 수 없습니다"):
            asyncio.run(tts.synthesize("테스트", tmp_path / "seg.audio"))
    finally:
        server.shutdown()


def test_missing_api_key_fails_at_startup(monkeypatch):
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CARTESIA_API_KEY"):
        make_tts("cartesia", voice=VOICE)


def test_missing_voice_fails_at_startup(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="보이스 ID"):
        make_tts("cartesia")


def test_list_voices_follows_pages():
    page1 = {"data": [{"id": "v1", "name": "지수", "gender": "feminine", "description": "차분함",
                       "accents": [{"locale": "ko-KR", "is_native": True}]}],
             "has_more": True, "next_page": "v1"}
    # 영어 원어민이 한국어도 말하는 다국어 보이스 — native가 아니다
    page2 = {"data": [{"id": "v2", "name": "Daniel", "accents": [
        {"locale": "en-US", "is_native": True}, {"locale": "ko-KR", "is_native": False}]}],
             "has_more": False, "next_page": None}
    base_url, received, server = _serve([(200, json.dumps(page1).encode()),
                                         (200, json.dumps(page2).encode())])
    try:
        voices = list_cartesia_voices(api_key="test-key", base_url=base_url)
    finally:
        server.shutdown()

    assert voices == [
        {"id": "v1", "name": "지수", "gender": "feminine", "description": "차분함", "native": True},
        {"id": "v2", "name": "Daniel", "gender": None, "description": "", "native": False},
    ]
    first, second = (parse_qs(urlparse(r["path"]).query) for r in received)
    assert first == {"language": ["ko"], "limit": ["100"]}
    assert second["starting_after"] == ["v1"]
    assert received[0]["version"] == CARTESIA_VERSION
