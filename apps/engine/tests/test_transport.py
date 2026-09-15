"""HttpTransport 관통 테스트 — 실제 HTTP 서버를 띄워 제출 페이로드를 확인한다.

백엔드 수신 엔드포인트(POST /api/engine/segments)는 백엔드 파트 구현 전이므로,
여기서는 계약대로 요청이 나가는지만 표준 라이브러리 서버로 검증한다.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

from onair_engine.domain import SegmentKind, SegmentSubmission
from onair_engine.transport import HttpTransport, make_transport

SEGMENT = SegmentSubmission(
    id="seg_test01", station_id="st_test", audio_ref="st_test/seg_test01.wav",
    duration_ms=4200, corner_type="filler", kind=SegmentKind.FILLER,
    priority=50, reorderable=True,
)


class _Recorder(BaseHTTPRequestHandler):
    received: ClassVar[list] = []
    status = 202

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "json": json.loads(body),
        })
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # 테스트 출력 오염 방지
        pass


def _serve():
    """요청을 기록하는 일회용 HTTP 서버를 띄우고 (base_url, 기록 리스트)를 준다."""
    _Recorder.received = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", _Recorder.received, server


def test_publish_segment_posts_contract_payload():
    base_url, received, server = _serve()
    try:
        transport = HttpTransport(base_url, token="test-token")
        asyncio.run(transport.publish_segment(SEGMENT))
    finally:
        server.shutdown()

    assert len(received) == 1
    call = received[0]
    assert call["path"] == "/api/engine/segments"
    assert call["auth"] == "Bearer test-token"
    # 설계 문서 5.1 계약 필드가 빠짐없이 실려야 한다
    payload = call["json"]
    assert payload["id"] == "seg_test01"
    assert payload["audio_ref"] == "st_test/seg_test01.wav"
    assert payload["duration_ms"] == 4200
    assert payload["kind"] == "filler"


def test_submit_failure_does_not_raise():
    """백엔드가 죽어 있어도 방송은 계속되어야 한다 — 예외가 위로 새지 않는다."""
    transport = HttpTransport("http://127.0.0.1:1", retries=1, timeout=0.5)
    asyncio.run(transport.publish_segment(SEGMENT))  # 예외 없이 반환되면 통과


def test_make_transport_selects_http():
    assert isinstance(make_transport("http", base_url="http://localhost:3000"), HttpTransport)
