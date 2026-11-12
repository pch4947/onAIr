"""StationEngine — 스테이션 1개의 컴포넌트 조립과 수명 관리 (설계 문서 3장)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from .audio import CachedTts
from .catalog import Catalog
from .corners.run import MaterialSources, TrackSource
from .domain import ListenerRequest, RequestState, StationConfig, new_id, wire_state
from .format import Clock, load_format
from .glue import GlueBank
from .inputs.classify import keywords
from .inputs.inbox import MessageInbox
from .inputs.router import BOARD_STORY
from .inputs.rules import load_chat_rules
from .inputs.stories import StoryPool
from .inputs.story_judge import StoryJudge
from .inputs.topic_plan import TopicPlan, angle_count
from .pipeline.llm import make_llm
from .pipeline.pipeline import GenerationPipeline
from .pipeline.safety import SafetyChecker
from .pipeline.tts import make_tts
from .scheduler.scheduler import Scheduler
from .sources.rss import Feed, RssCollector, load_news_feeds
from .telemetry import Telemetry
from .transport import Transport


@dataclass
class EngineSettings:
    """스테이션과 무관한 엔진 인프라 설정 (설정 파일의 station 외 항목)."""

    transport_kind: str = "stdout"
    transport_base_url: str = "http://localhost:3000"  # transport_kind="http"일 때 백엔드 주소
    transport_token: str | None = None  # 내부 통신 토큰 — 환경변수로 주입한다
    transport_redis_url: str = "redis://localhost:6379/0"  # transport_kind="redis"일 때
    audio_dir: Path = field(default_factory=lambda: Path("var/audio"))  # 공유 오디오 루트
    sqlite_path: Path = field(default_factory=lambda: Path("var/engine_metrics.sqlite"))
    safety_rules_path: Path = field(default_factory=lambda: Path("config/safety_rules.yaml"))
    chat_rules_path: Path = field(default_factory=lambda: Path("config/chat_rules.yaml"))
    news_feeds_path: Path = field(default_factory=lambda: Path("config/news_feeds.yaml"))
    formats_dir: Path = field(default_factory=lambda: Path("config/formats"))  # 형식·코너 정의
    llm: str = "dummy"
    llm_model: str | None = None  # None이면 어댑터 기본 모델 (openai는 필수)
    llm_base_url: str | None = None  # openai 호환 서버 주소. None이면 OpenAI 본가
    llm_reasoning_effort: str | None = None  # openai 추론 모델의 추론량 (지연 조절)
    tts: str = "dummy"
    tts_voice: str | None = None  # None이면 어댑터 기본 보이스
    tts_cache_dir: Path | None = None  # None이면 캐시 없이 매번 합성. 공유 오디오 루트 밖에 둔다
    max_concurrent_generations: int = 2
    # 백엔드가 track.queued를 보내기 전에 더미 곡 이름으로 곡 소개를 만들지. 로컬 실행용이다 —
    # 운영(--serve)에서는 끈다: 실제로 나오지 않는 곡을 소개하게 된다
    dummy_tracks: bool = True
    target_buffer_sec: float = 30.0
    # 켜면 코너 진입을 백엔드에 알린다. 지금 백엔드는 모르는 이벤트를 격리하므로 #170 전까지 끈다
    emit_corner_changed: bool = False
    # 엔진→백엔드 이벤트의 계약 버전. 2면 segment.submitted에 input_refs·reflection_levels를, request.state에
    # v2 값을 보낸다 (계약 10.4·10.5절). 백엔드가 v1·v2를 함께 받게 배포된 뒤에 올린다 (#157)
    contract_version: int = 1
    # 이보다 오래된 request.arrived는 답하지 않는다. 입력 스트림을 처음부터 읽으므로
    # 엔진이 꺼져 있던 사이의 예전 요청이 재기동 때 몰려온다 [예시]
    request_max_age_sec: float = 600.0
    # 엔진 REST (계약 6장) — 단일 VM에서 백엔드만 접근하므로 루프백에만 연다. 포트는 [예시]
    api_host: str = "127.0.0.1"
    api_port: int = 8100
    draft_timeout_sec: float = 30.0  # persona 초안 생성 상한 — 응답 목표는 15초 [예시]


class StationRejected(Exception):
    """방송을 시작할 수 없다 — station.rejected로 백엔드에 돌려보낸다 (계약 3.1절)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class StationEngine:
    def __init__(self, config: StationConfig, settings: EngineSettings, transport: Transport):
        self.config = config
        self.settings = settings
        self.transport = transport

        # 키·보이스 누락·형식 설정 오류는 방송 도중이 아니라 여기서 실패한다 — 계측 DB를 열기 전에 확인한다
        try:
            fmt = load_format(config.format, settings.formats_dir)
            clock = Clock(fmt, main_corners=config.corners, sections=config.news_sections)
            intensity = fmt.intensity[config.intensity]
            chat_rules = load_chat_rules(settings.chat_rules_path)
            news_feeds = load_news_feeds(settings.news_feeds_path) if fmt.format == "news" else None
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise StationRejected("invalid_payload", f"형식 설정: {exc}"[:300]) from exc
        try:
            # 보이스는 방마다 다르다 (persona.md 9.3절). 엔진 설정 값은 로컬 실행용 기본값이다
            tts_client = make_tts(settings.tts, voice=config.voice or settings.tts_voice)
        except RuntimeError as exc:
            raise StationRejected("tts_unavailable", str(exc)) from exc
        try:
            llm = make_llm(settings.llm, model=settings.llm_model,
                           base_url=settings.llm_base_url,
                           reasoning_effort=settings.llm_reasoning_effort)
        except RuntimeError as exc:
            raise StationRejected("llm_unavailable", str(exc)) from exc

        self.telemetry = Telemetry(settings.sqlite_path, config.station_id)
        self.telemetry.corner_titles = {"filler": "대체 멘트",
                                        **{name: spec.title for name, spec in fmt.corners.items()}}
        if config.snapshot is not None:
            self.telemetry.log_station("station.created", config.snapshot)
        # 제출 페이로드의 audio_ref는 이 루트 기준 상대 경로로 나간다 (설계 문서 5.1)
        audio_root = Path(settings.audio_dir)
        # 파이프라인과 접착 요소 모두 원자적 쓰기·캐시·길이 실측을 거친다
        tts = CachedTts(tts_client, cache_dir=settings.tts_cache_dir)
        safety = SafetyChecker(settings.safety_rules_path)
        pipeline = GenerationPipeline(
            station_id=config.station_id, profile=config.profile,
            llm=llm, tts=tts, safety=safety,
            telemetry=self.telemetry, audio_root=audio_root,
            recent_segments=config.recent_segments,
        )
        self.tracks = TrackSource(Catalog(), dummy=settings.dummy_tracks)
        self.rss = RssCollector()
        if news_feeds is not None:
            # 호스트가 고른 분야만 폴링한다. section은 코너의 label(분야 이름)과 맞춘다
            self.rss = RssCollector(
                feeds=[Feed(entry.source, entry.url, fmt.section_names.get(code, code))
                       for code in config.news_sections or fmt.default_sections
                       for entry in news_feeds.feeds.get(code, [])],
                poll_sec=news_feeds.poll_sec, max_age_sec=news_feeds.max_age_hours * 3600,
                violates=safety.check_l0, on_refresh=self.telemetry.log_rss,
            )
        self.sources = MaterialSources(
            tracks=self.tracks, rss=self.rss,
            inbox=MessageInbox(max_chars=chat_rules.message_max_chars, violates=safety.check_l2),
            stories=StoryPool(topic_words=keywords(self.topic), violates=safety.check_l2, judged=True),
        )
        self.story_judge = StoryJudge(llm, self.topic, self.sources.stories, telemetry=self.telemetry)
        # 호스트가 정한 주제가 있고 이 형식에 주제 토크가 있을 때만 — 컨셉을 갈래로 펼치지는 않는다
        talks = any({s.kind, s.again} & {"talk_open", "talk_resume"}
                    for c in fmt.corners.values() for s in c.steps)
        self.topic_plan = TopicPlan(
            llm, config.topic, count=angle_count(config.broadcast_minutes), violates=safety.check_l2,
            forbidden=config.profile.forbidden, telemetry=self.telemetry,
        ) if config.topic and talks else None
        self.glue = GlueBank(tts=tts, audio_root=audio_root, station_id=config.station_id,
                             profile=config.profile, fmt=fmt, safety=safety)
        self.scheduler = Scheduler(
            config=config, fmt=fmt, clock=clock, sources=self.sources, intensity=intensity,
            topic=self.topic, pipeline=pipeline, glue=self.glue,
            transport=transport, telemetry=self.telemetry,
            chat_rules=chat_rules, violates=safety.check_l2,
            target_buffer_sec=settings.target_buffer_sec,
            max_concurrent=settings.max_concurrent_generations,
            emit_corner_changed=settings.emit_corner_changed, topic_plan=self.topic_plan,
            contract_version=settings.contract_version,
        )
        self._contract_version = settings.contract_version
        self._safety = safety
        self._stop = asyncio.Event()

    @property
    def topic(self) -> str:
        """이 방송의 확정된 주제 — station.started로 백엔드에 알린다 (백엔드는 이 주제로 사연 후보를 고른다).

        TODO(오프닝 코너): 호스트가 비웠으면 컨셉에서 LLM으로 만든다. 지금은 컨셉을 그대로 쓴다.
        """
        return self.config.topic or self.config.profile.concept

    async def run(self, max_segments: int | None = None) -> None:
        # 수집은 사전 렌더링과 동시에 시작한다 — 방송 시작을 피드 시간만큼 더 늦추지 않는다
        poller = asyncio.create_task(self.rss.run()) if self.rss.feeds else None
        # 갈래 계획도 기다리지 않는다 — 오프닝과 첫 곡이 나가는 동안 끝나면 첫 주제 토크부터 쓴다
        plan = asyncio.create_task(self.topic_plan.run()) if self.topic_plan else None
        judge: asyncio.Task | None = None
        try:
            # 접착 요소는 방송 시작 전에 이 스테이션의 보이스로 사전 렌더링한다 (설계 문서 4.5)
            try:
                await self.glue.prerender()
            except Exception as exc:  # 보이스 ID 오류·TTS 장애 — 첫 합성에서 드러난다
                await self.transport.notify_station("station.rejected", {
                    "reason": "tts_unavailable",
                    "detail": f"접착 요소 사전 렌더링 실패: {exc}"[:300],
                })
                raise StationRejected("tts_unavailable", str(exc)) from exc
            if poller is not None:
                await self.rss.ready.wait()  # 수집이 실패해도 방송은 시작한다 — 뉴스 코너만 건너뛴다
            # 시작 전에 받은 사연은 첫 사연 코너 전에 판정을 끝낸다. 백엔드는 보통 station.started 뒤에 보낸다
            await self.story_judge.judge_pending()
            judge = asyncio.create_task(self.story_judge.run())
            await self.transport.notify_station("station.started", {"topic": self.topic})
            self.telemetry.log_station("station.started", {"topic": self.topic})
            await self.scheduler.run(self._stop, max_segments=max_segments)
        finally:
            tasks = [t for t in (poller, plan, judge) if t is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.telemetry.close()

    def stop(self) -> None:
        self._stop.set()

    def report_backpressure(self, payload: dict, at: float | None = None) -> None:
        """백엔드의 실제 남은 방송 분량 — 편성 관리자가 생성량을 정하는 기준이 된다 (계약 3.2절)."""
        d_total_ms = payload.get("d_total_ms")
        if not isinstance(d_total_ms, (int, float)):  # 백엔드가 큐를 못 읽은 주기 — 이전 보고를 유지
            return
        self.scheduler.report_backpressure(d_total_ms, str(payload.get("severity", "")), at)

    def on_track_queued(self, payload: dict) -> None:
        """다음에 틀 곡 (계약 3.4절) — 다음 곡 소개 멘트가 이 곡을 소개한다 (music_ref)."""
        track = payload.get("track")
        if not isinstance(track, dict) or not track.get("track_ref") or not track.get("title"):
            raise ValueError(f"track.queued에 곡 정보가 없다: {payload!r}"[:200])
        track = dict(track)
        if payload.get("source") == "request":  # 신청곡은 곡 연속 방지로 미루지 않는다 (#228)
            track["requested"] = True
        nickname = payload.get("nickname")
        if payload.get("source") == "request" and isinstance(nickname, str):
            # Untrusted requester names must pass the same input filter as chat.
            if nickname and self._safety.check_l0(nickname) is None:
                track["requester_nickname"] = nickname[:50]
        if payload.get("source") == "request" and payload.get("request_id"):
            self.scheduler.attach_song_request(track, payload["request_id"])
        self.tracks.queue(track)
        self.telemetry.log_track(track)

    def on_board_story(self, payload: dict) -> None:
        """게시판 사연 후보 (계약 3.5절). L0를 통과한 것만 풀에 넣는다. 내부 ID는 프롬프트에 넣지 않는다."""
        story_id, body = payload.get("story_id"), payload.get("body")
        if not isinstance(story_id, str) or not isinstance(body, str) or not body.strip():
            raise ValueError(f"board.story 형식 오류: {payload!r}"[:200])
        if self._safety.check_l0(body) is not None:
            self.telemetry.log_request_state(story_id, RequestState.REJECTED)
            return
        posted_at = payload.get("posted_at")
        tags = payload.get("tags")
        self.scheduler.add_story(ListenerRequest(
            request_id=story_id, kind=BOARD_STORY, body=body,
            requester_ref=str(payload.get("author_ref") or "anonymous"),
            received_at=float(posted_at) if isinstance(posted_at, (int, float)) else time.time(),
            nickname=self._safe_nickname(payload.get("nickname")),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
        ))

    def on_board_story_withdrawn(self, payload: dict) -> None:
        if isinstance(story_id := payload.get("story_id"), str):
            self.scheduler.withdraw_story(story_id)

    async def on_request_cancelled(self, payload: dict) -> None:
        if isinstance(request_id := payload.get("request_id"), str):
            await self.scheduler.cancel_request(request_id)

    def on_corner_skip(self, payload: dict) -> None:
        """운영자가 고른 코너로 넘어간다 (계약 3.2절 corner.skip). 있는 코너인지는 편성 관리자가 가린다."""
        corner = payload.get("corner")
        if not isinstance(corner, str) or not corner:
            raise ValueError(f"corner.skip에 코너가 없다: {payload!r}"[:200])
        self.scheduler.skip_to(corner)

    async def on_segments_discarded(self, payload: dict) -> None:
        """백엔드가 재생하지 않고 버린 멘트 (계약 3.2절 segment.discarded)."""
        ids = payload.get("segment_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError(f"segment.discarded 형식 오류: {payload!r}"[:200])
        await self.scheduler.discard_segments(ids)

    def on_segment_published(self, segment_id: object, at: object) -> None:
        if isinstance(segment_id, str) and isinstance(at, (int, float)):
            self.scheduler.on_segment_published(segment_id, float(at))

    def _safe_nickname(self, nickname: object) -> str | None:
        """방송에서 부를 이름 — 외부 텍스트이므로 L0를 통과해야 한다. 아니면 익명으로 읽는다."""
        if not isinstance(nickname, str) or not nickname.strip():
            return None
        return nickname.strip()[:50] if self._safety.check_l0(nickname) is None else None

    def on_track_started(self, payload: dict) -> None:
        """곡 재생 시작 — 송출 위치를 보정한다. 곡 길이는 소개 멘트를 낼 때 이미 버퍼에 더했다."""
        self.telemetry.log_station("track.started", payload)
        track_ref, started_at = payload.get("track_ref"), payload.get("started_at")
        if isinstance(track_ref, str) and isinstance(started_at, (int, float)):
            self.scheduler.on_track_started(track_ref, float(started_at))

    def on_segment_started(self, segment_id: object, started_at: object) -> None:
        """세그먼트 재생 시작 (계약 10.6절) — 인용이 들어 있으면 여기서 반영이 확정된다."""
        if isinstance(segment_id, str) and isinstance(started_at, (int, float)):
            self.scheduler.on_segment_started(segment_id, float(started_at))

    async def _notify_state(self, request_id: str, state: RequestState) -> None:
        wire = wire_state(state, self._contract_version)
        if wire is not None:  # v2에는 screened가 없다 — 바로 뒤에 pooled/inboxed가 나간다
            await self.transport.notify_request_state(request_id, wire)

    async def reject_stale_request(self, request_id: str) -> None:
        """너무 늦게 받은 요청 — 답하지 않고 종결한다. 백엔드 상태가 requested로 남지 않게 rejected로 알린다."""
        self.telemetry.log_request_state(request_id, RequestState.REJECTED)
        await self._notify_state(request_id, RequestState.REJECTED)

    async def submit_request(self, kind: str, body: str, requester_ref: str,
                             request_id: str | None = None,
                             nickname: object = None) -> ListenerRequest:
        """청취자 요청 수신 — L0 입력단 검사 후 큐 적재 (설계 문서 4.6).

        백엔드 request.arrived 이벤트로 들어온 요청은 백엔드가 발급한 request_id를 그대로 쓴다.
        상태 통보를 백엔드가 자기 요청과 대응시킬 수 있어야 하기 때문이다. 데모 요청만 엔진이 발급한다.
        """
        req = ListenerRequest(
            request_id=request_id or new_id("req"), kind=kind, body=body,
            requester_ref=requester_ref, received_at=time.time(),
            nickname=self._safe_nickname(nickname),
        )
        if self._safety.check_l0(body) is not None:
            req.state = RequestState.REJECTED
            self.telemetry.log_request_state(req.request_id, req.state)
            await self._notify_state(req.request_id, req.state)  # F-15
            return req
        req.state = RequestState.SCREENED
        self.telemetry.log_request_state(req.request_id, req.state)
        await self._notify_state(req.request_id, req.state)
        await self.scheduler.accept_request(req)
        return req
