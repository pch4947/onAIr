"""GoogleTtsClient — 로컬 HTTP 서버로 REST 요청 형식과 응답 처리를 확인한다 (실제 API 호출 없음)."""
import asyncio
import base64
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from onair_engine.pipeline.tts import GoogleTtsClient, make_tts


def _wav_bytes(ms: int, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate * ms // 1000))
    return buf.getvalue()


def _serve(status: int, body: dict):
    """고정 응답을 돌려주고 요청을 기록하는 일회용 서버 — (endpoint, 기록, 서버)."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            received.append({"key": self.headers.get("X-Goog-Api-Key"), "json": json.loads(raw)})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):  # 테스트 출력 오염 방지
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/v1/text:synthesize", received, server


def test_synthesize_sends_rest_request_and_writes_audio(tmp_path):
    audio = base64.b64encode(_wav_bytes(1500)).decode()
    endpoint, received, server = _serve(200, {"audioContent": audio})
    try:
        tts = GoogleTtsClient("ko-KR-Chirp3-HD-Aoede", api_key="test-key", endpoint=endpoint)
        duration_ms = asyncio.run(tts.synthesize("안녕하세요, 새벽입니다.", tmp_path / "seg.audio"))
    finally:
        server.shutdown()

    assert duration_ms == 1500
    assert (tmp_path / "seg.audio").read_bytes() == base64.b64decode(audio)
    assert received[0]["key"] == "test-key"
    assert received[0]["json"] == {
        "input": {"text": "안녕하세요, 새벽입니다."},
        "voice": {"languageCode": "ko-KR", "name": "ko-KR-Chirp3-HD-Aoede"},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": 1.0},
    }


def test_api_error_is_raised_with_detail(tmp_path):
    endpoint, _, server = _serve(403, {"error": {"message": "API key not valid"}})
    try:
        tts = GoogleTtsClient(api_key="bad-key", endpoint=endpoint)
        with pytest.raises(RuntimeError, match="403.*API key not valid"):
            asyncio.run(tts.synthesize("테스트", tmp_path / "seg.audio"))
    finally:
        server.shutdown()


def test_missing_api_key_fails_at_startup(monkeypatch):
    monkeypatch.delenv("GOOGLE_TTS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_TTS_API_KEY"):
        make_tts("google")
