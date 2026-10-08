"""LLM 어댑터와 실패 처리 — 로컬 HTTP 서버로 REST 형식을 확인한다 (실제 API 호출 없음)."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from onair_engine.catalog import Catalog
from onair_engine.corners import build_corners
from onair_engine.domain import (
    GenerationJob,
    ListenerRequest,
    Material,
    Prompt,
    RequestState,
    Script,
    SegmentKind,
    StationConfig,
    StationProfile,
)
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.pipeline.llm import (
    REJECT,
    ClaudeLlmClient,
    GeminiLlmClient,
    LlmError,
    OpenAiLlmClient,
    clean_script,
    make_llm,
)
from onair_engine.pipeline.pipeline import GenerationPipeline
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.pipeline.tts import make_tts
from onair_engine.sources.rss import RssCollector
from onair_engine.telemetry import Telemetry

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")
PROMPT = Prompt(system="당신은 DJ다.", user="짧은 브릿지 멘트를 작성하라.")


def _serve(status: int, body: dict):
    """고정 응답을 돌려주고 요청을 기록하는 일회용 서버 — (base_url, 기록, 서버)."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            received.append({"path": self.path, "headers": self.headers,  # 대소문자 무시 조회
                             "json": json.loads(raw)})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):  # 테스트 출력 오염 방지
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}", received, server


def _call(client, prompt=PROMPT) -> str:
    return asyncio.run(client.generate(prompt)).text


def test_claude_sends_messages_request_and_cleans_output():
    base, received, server = _serve(200, {
        "content": [{"type": "text", "text": "**DJ 테스트:** \"잠깐 쉬어가요. (잔잔한 음악) 곧 돌아올게요.\""}],
        "stop_reason": "end_turn",
    })
    try:
        client = ClaudeLlmClient("claude-test", api_key="k", endpoint=base + "/v1/messages")
        text = _call(client)
    finally:
        server.shutdown()

    assert text == "잠깐 쉬어가요. 곧 돌아올게요."
    req = received[0]
    assert req["headers"]["x-api-key"] == "k"
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    # urllib 기본 UA는 Cloudflare를 앞에 둔 API가 403(error code 1010)으로 막는다
    assert req["headers"]["User-Agent"].startswith("onair-engine/")
    assert req["json"]["model"] == "claude-test"
    assert req["json"]["system"] == PROMPT.system
    assert req["json"]["messages"] == [{"role": "user", "content": PROMPT.user}]


def test_claude_truncated_output_keeps_complete_sentences():
    base, _, server = _serve(200, {
        "content": [{"type": "text", "text": "오늘도 고생 많으셨어요. 이제 다음 곡으로 넘어가"}],
        "stop_reason": "max_tokens",
    })
    try:
        text = _call(ClaudeLlmClient(api_key="k", endpoint=base))
    finally:
        server.shutdown()
    assert text == "오늘도 고생 많으셨어요."


def test_gemini_sends_generate_content_and_disables_thinking():
    base, received, server = _serve(200, {
        "candidates": [{"content": {"parts": [{"text": "비 오는 밤이네요."}]},
                        "finishReason": "STOP"}],
    })
    try:
        client = GeminiLlmClient("gemini-test", api_key="k",
                                 endpoint=base + "/v1beta/models/{model}:generateContent")
        text = _call(client)
    finally:
        server.shutdown()

    assert text == "비 오는 밤이네요."
    req = received[0]
    assert req["path"] == "/v1beta/models/gemini-test:generateContent"
    assert req["headers"]["x-goog-api-key"] == "k"
    assert req["json"]["systemInstruction"] == {"parts": [{"text": PROMPT.system}]}
    assert req["json"]["contents"] == [{"role": "user", "parts": [{"text": PROMPT.user}]}]
    assert req["json"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_gemini_safety_block_becomes_reject():
    base, _, server = _serve(200, {"promptFeedback": {"blockReason": "SAFETY"}})
    try:
        text = _call(GeminiLlmClient(api_key="k", endpoint=base + "/{model}"))
    finally:
        server.shutdown()
    assert text == REJECT


def test_http_error_raises_llm_error_with_detail():
    base, _, server = _serve(401, {"error": {"message": "invalid x-api-key"}})
    try:
        with pytest.raises(LlmError, match="claude 401.*invalid x-api-key"):
            _call(ClaudeLlmClient(api_key="bad", endpoint=base))
    finally:
        server.shutdown()


def test_clean_script_normalizes_reject_and_rejects_empty():
    assert clean_script(" reject. ") == REJECT
    assert clean_script("사연: 잠이 안 와요. 저도 그래요.") == "사연: 잠이 안 와요. 저도 그래요."
    with pytest.raises(LlmError):
        clean_script("  **  ")


def test_make_llm_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        make_llm("claude")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        make_llm("gemini")


class FlakyLlm:
    """앞의 fail_times번은 실패하고 이후 고정 대본을 돌려준다. 받은 프롬프트를 기록한다."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.prompts: list[Prompt] = []

    async def generate(self, prompt: Prompt) -> Script:
        self.prompts.append(prompt)
        if len(self.prompts) <= self.fail_times:
            raise LlmError("claude 529: overloaded")
        return Script(text=f"실제 대본 {len(self.prompts)}번입니다.")


def _pipeline(tmp_path, llm) -> GenerationPipeline:
    return GenerationPipeline(
        station_id="st_test", profile=PROFILE,
        corners=build_corners(catalog=Catalog(), rss=RssCollector()),
        llm=llm, tts=make_tts("dummy"), safety=SafetyChecker(RULES),
        telemetry=Telemetry(tmp_path / "metrics.sqlite", "st_test"),
        audio_root=tmp_path / "audio", recent_segments=2,
    )


def _opening_job():
    return GenerationJob(job_id="job_t", corner_type="opening", material=Material())


def test_pipeline_retries_once_then_succeeds(tmp_path):
    llm = FlakyLlm(fail_times=1)
    sub = asyncio.run(_pipeline(tmp_path, llm).run(_opening_job()))
    assert sub is not None and sub.corner_type == "opening"
    assert len(llm.prompts) == 2


def test_pipeline_falls_back_to_filler_after_two_failures(tmp_path):
    llm = FlakyLlm(fail_times=99)
    sub = asyncio.run(_pipeline(tmp_path, llm).run(_opening_job()))
    assert len(llm.prompts) == 2
    assert sub is not None
    assert sub.corner_type == "filler" and sub.kind == SegmentKind.FILLER


def test_pipeline_feeds_recent_scripts_back_as_context(tmp_path):
    llm = FlakyLlm(fail_times=0)
    pipeline = _pipeline(tmp_path, llm)

    async def three():
        for _ in range(3):
            await pipeline.run(_opening_job())

    asyncio.run(three())
    assert "직전에 송출한 멘트" not in llm.prompts[0].system
    last = llm.prompts[2].system
    assert "실제 대본 1번입니다." in last and "실제 대본 2번입니다." in last
    # 코너 판별은 user만 본다 — 맥락이 user에 섞이면 더미 폴백의 코너 판별이 깨진다
    assert "실제 대본" not in llm.prompts[2].user


def test_failed_request_is_requeued_once_then_rejected(tmp_path):
    config = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=1)
    settings = EngineSettings(audio_dir=tmp_path / "audio",
                              sqlite_path=tmp_path / "metrics.sqlite", safety_rules_path=RULES)

    class Capture:
        def __init__(self):
            self.segments, self.states = [], []

        async def publish_segment(self, sub):
            self.segments.append(sub)

        async def notify_request_state(self, request_id, state):
            self.states.append((request_id, state))

        async def notify_station(self, event_type, payload):
            pass

    transport = Capture()
    engine = StationEngine(config, settings, transport)
    engine.scheduler.pipeline.llm = FlakyLlm(fail_times=99)  # 제공자 전면 장애

    async def scenario() -> ListenerRequest:
        req = await engine.submit_request("story", "요즘 잠이 안 와요", "tester")
        await engine.run(max_segments=4)
        return req

    req = asyncio.run(scenario())
    states = [s for rid, s in transport.states if rid == req.request_id]
    assert states[-1] == RequestState.REJECTED
    assert states.count(RequestState.QUEUED) == 1  # 되돌림은 한 번
    assert RequestState.GENERATED not in states
    assert [s.kind for s in transport.segments].count(SegmentKind.ACK) == 1
    assert all(s.corner_type == "filler" for s in transport.segments
               if s.kind != SegmentKind.ACK), "장애 중에도 filler로 방송은 이어진다"


def _openai_reply(content, finish_reason="stop", refusal=None):
    return {"choices": [{"message": {"role": "assistant", "content": content,
                                     "refusal": refusal},
                         "finish_reason": finish_reason}]}


def test_openai_compatible_server_gets_max_tokens_and_no_auth(monkeypatch):
    # 개발 환경에 키가 있으면 호환 서버에도 실린다 — "키 없이"를 확인하려면 비워야 한다
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    base, received, server = _serve(200, _openai_reply("오늘 밤도 함께해요."))
    try:
        client = OpenAiLlmClient("qwen3:8b", base_url=base + "/v1", temperature=0.7)
        text = _call(client)
    finally:
        server.shutdown()

    assert text == "오늘 밤도 함께해요."
    req = received[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["Authorization"] is None  # Ollama 같은 호환 서버는 키 없이
    assert req["json"]["messages"] == [{"role": "system", "content": PROMPT.system},
                                       {"role": "user", "content": PROMPT.user}]
    assert req["json"]["max_tokens"] == 400 and req["json"]["temperature"] == 0.7
    assert "reasoning_effort" not in req["json"]


def test_openai_official_uses_max_completion_tokens(monkeypatch):
    monkeypatch.setattr("onair_engine.pipeline.llm.DEFAULT_OPENAI_BASE_URL", "http://x/v1")
    client = OpenAiLlmClient("m", base_url="http://x/v1", api_key="k", reasoning_effort="low")
    captured = {}

    def fake_post(url, body, headers, timeout, provider):
        captured.update(url=url, body=body, headers=headers)
        return _openai_reply("안녕하세요.")

    monkeypatch.setattr("onair_engine.pipeline.llm._post_json", fake_post)
    assert _call(client) == "안녕하세요."
    assert captured["headers"] == {"Authorization": "Bearer k"}
    assert captured["body"]["max_completion_tokens"] == 400
    assert "max_tokens" not in captured["body"] and "temperature" not in captured["body"]
    assert captured["body"]["reasoning_effort"] == "low"


def test_openai_refusal_and_filter_become_reject():
    for reply in (_openai_reply(None, refusal="I can't help with that."),
                  _openai_reply("", finish_reason="content_filter")):
        base, _, server = _serve(200, reply)
        try:
            assert _call(OpenAiLlmClient("m", base_url=base)) == REJECT
        finally:
            server.shutdown()


def test_openai_reasoning_exhausting_limit_is_an_error():
    base, _, server = _serve(200, _openai_reply("", finish_reason="length"))
    try:
        with pytest.raises(LlmError, match="reasoning_effort"):
            _call(OpenAiLlmClient("m", base_url=base))
    finally:
        server.shutdown()


def test_openai_requires_model_and_official_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="모델 ID"):
        make_llm("openai")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        make_llm("openai", model="some-model")
    make_llm("openai", model="qwen3:8b", base_url="http://localhost:11434/v1")  # 키 없이 OK
