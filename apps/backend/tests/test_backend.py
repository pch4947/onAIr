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
        self.assertEqual(self.call("/api/requests")[1], {"requests": []})
        self.assertEqual(self.call("/api/stream/state")[1]["stream"]["status"], "bootstrapping")
        result = self.call("/api/scheduler/tick", {})[1]
        self.assertEqual(result["decision"]["action"], "build_buffer")
        result = self.call("/api/scheduler/tick", {"bufferSeconds": 60})[1]
        self.assertEqual(result["decision"]["action"], "idle")
        self.assertEqual(result["stream"]["status"], "ready")
        status, payload = self.call("/api/requests", {"prompt": "노래 부탁해요"})
        self.assertEqual(status, 202)
        entry = payload["request"]
        self.assertEqual(entry["listenerId"], "anonymous")
        self.assertEqual(entry["status"], "queued")
        self.assertEqual(self.call("/api/requests")[1]["requests"], [entry])
        result = self.call("/api/scheduler/tick", {})[1]
        self.assertEqual(result["decision"]["action"], "serve_request")
        self.assertEqual(result["pendingRequestCount"], 1)
        self.assertEqual(result["stream"]["bufferSeconds"], 60)

    def test_invalid_inputs_and_absent_ingestion(self):
        for body in [{}, {"prompt": ""}, {"prompt": 5}]:
            self.assertEqual(self.call("/api/requests", body)[0], 422)
        for body in [{"bufferSeconds": -1}, {"bufferSeconds": "NaN"}, {"bufferTargetSeconds": 0}]:
            self.assertEqual(self.call("/api/scheduler/tick", body)[0], 422)
        self.assertEqual(self.call("/api/segments", {})[0], 404)

    def test_cors_and_openapi(self):
        request = Request(self.base + "/api/requests", method="OPTIONS", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type"})
        with self.client.open(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(set(self.call("/openapi.json")[1]["paths"]),
                         {"/health", "/api/requests", "/api/stream/state", "/api/scheduler/tick"})

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
