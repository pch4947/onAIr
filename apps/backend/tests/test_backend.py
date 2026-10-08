"""Real HTTP regression checks without extra test dependencies."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

from app.scheduler import decide_next_segment


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.client = build_opener(ProxyHandler({}))
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "app"], cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, HOST="127.0.0.1", PORT=str(port),
                     ONAIR_STATION_ID="st_api_test", ONAIR_HLS_ENABLED="0",
                     ONAIR_REDIS_URL="redis://127.0.0.1:1/0",
                     STREAM_BUFFER_TARGET_SECONDS="45", REQUEST_RESPONSE_WINDOW_SECONDS="90"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.stop_server)
        for _ in range(100):
            if cls.server.poll() is not None:
                raise RuntimeError("Server exited")
            try:
                cls.client.open(cls.base + "/health", timeout=1).close()
                return
            except URLError:
                time.sleep(0.1)
        raise RuntimeError("Server startup timeout")

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)

    def call(self, path, data=None):
        req = Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                      headers={"Content-Type": "application/json"})
        try:
            response = self.client.open(req, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_request_and_policy_flow(self):
        self.assertEqual(self.call("/health"), (200, {"ok": True, "service": "onAIr backend"}))
        self.assertEqual(self.call("/api/requests")[0], 503)
        self.assertEqual(self.call("/api/stream/state")[1]["stream"]["status"], "bootstrapping")
        result = self.call("/api/stream/state")[1]
        self.assertIsNone(result["pendingRequestCount"])
        self.assertFalse(result["requestStatsAvailable"])
        self.assertEqual(self.call("/api/scheduler/tick", {})[0], 503)
        self.assertEqual(self.call("/api/requests", {"prompt": "노래 부탁해요"})[0], 503)

    def test_invalid_inputs_and_absent_ingestion(self):
        for body in [{}, {"prompt": ""}, {"prompt": 5}]:
            self.assertEqual(self.call("/api/requests", body)[0], 422)
        for body in [{"bufferSeconds": -1}, {"bufferSeconds": "NaN"}, {"bufferTargetSeconds": 0}]:
            self.assertEqual(self.call("/api/scheduler/tick", body)[0], 422)
        self.assertEqual(self.call("/api/segments", {})[0], 404)

    def test_playback_fragment_validation(self):
        fragment = "a" * 32 + "_" + "b" * 32 + "_0.ts"
        status, result = self.call("/api/stream/state?fragment=" + fragment)
        self.assertEqual(status, 200)
        self.assertEqual(result["stream"]["playback"], {
            "fragment": fragment, "status": "unavailable", "segment": None})
        self.assertIsNone(result["stream"]["currentSegment"])
        self.assertNotIn("playback", self.call("/api/stream/state")[1]["stream"])
        for invalid in ["../secret", "", "a" * 129]:
            self.assertEqual(self.call("/api/stream/state?fragment=" + invalid)[0], 422)
        with self.client.open(self.base + "/api/stream/state?fragment=" + fragment) as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_cors_and_openapi(self):
        request = Request(self.base + "/api/requests", method="OPTIONS", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type"})
        with self.client.open(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(set(self.call("/openapi.json")[1]["paths"]),
                         {"/health", "/health/redis", "/api/requests", "/api/stream/state", "/api/scheduler/tick",
                          "/api/requests/{request_id}/history", "/api/broadcast/segments/{segment_id}/state",
                          "/api/broadcast/queue", "/api/requests/{request_id}/state", "/hls/{station_id}/{filename}"})

    def test_urgent_request_and_unsafe_buffer(self):
        args = dict(buffer_seconds=40, estimated_generation_seconds=30,
                    oldest_request_age_seconds=80, pending_request_count=1,
                    buffer_target_seconds=45, response_window_seconds=90)
        decision = decide_next_segment(**args)
        self.assertEqual(decision["action"], "serve_request")
        self.assertEqual(decision["metrics"], {"priority": 1, "underrunRisk": 0.75,
                                             "bufferDeficit": 0.11, "requestPressure": 0.89})
        self.assertEqual(decide_next_segment(**{**args, "buffer_seconds": 5})["action"], "build_buffer")
        self.assertEqual(decide_next_segment(**{**args, "buffer_seconds": 0.5,
            "estimated_generation_seconds": 0, "buffer_target_seconds": 1})["action"], "build_buffer")
