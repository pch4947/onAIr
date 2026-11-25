"""StationEngine — 스테이션 1개의 컴포넌트 조립과 수명 관리 (설계 문서 3장)."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from onair_schema import Persona, error_detail
from pydantic import BaseModel, ValidationError

from .audio import CachedTts
from .catalog import Catalog
from .corners.run import MaterialSources, TrackSource
from .domain import ListenerRequest, RequestState, StationConfig, StationProfile, new_id, wire_state
from .format import Clock, load_format
from .glue import GlueBank
from .inputs.chat_tone import ChatToneJudge
from .inputs.classify import keywords
from .inputs.dilemmas import DilemmaPlan
from .inputs.inbox import MessageInbox
from .inputs.router import BOARD_STORY
from .inputs.rules import load_chat_rules
from .inputs.song_fit import SongFitJudge
from .inputs.song_picks import Pick, SongPicker, confirmed_names
from .inputs.stories import StoryPool
from .inputs.story_judge import StoryJudge
from .inputs.talk_bank import TalkBank, persona_text, shared_bank
from .inputs.topic_plan import TopicPlan, angle_count
from .inputs.topic_usage import DAYS as TOPIC_REUSE_DAYS
from .inputs.topic_usage import TopicUsage
from .personas import unsafe_fields
from .pipeline.embed import make_embedder
from .pipeline.llm import make_llm
from .pipeline.pipeline import GenerationPipeline
from .pipeline.safety import SafetyChecker
from .pipeline.tts import TtsClient, make_tts, voice_style
from .scheduler.scheduler import FormatSwitch, PersonaSwitch, Scheduler
from .sources.rss import Feed, RssCollector, load_news_feeds
from .telemetry import Telemetry
from .transport import Transport

logger = logging.getLogger(__name__)


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
    pause_ms: int = 0  # 멘트 끝에 붙이는 쉬는 시간(무음). WAV를 내는 TTS(gemini·dummy)에만 붙는다
    hedge: bool = True  # 느린 LLM·TTS 호출에 예비 호출을 겹쳐 보낸다 (#340)
    tts_split_chars: int = 0  # 이보다 긴 대본은 나눠서 동시에 합성하고 한 파일로 붙인다 (#342). 0이면 나누지 않는다
    # TTS 말투 지시와 쉼 태그 (#369) — 받을 수 있는 TTS(gemini)에만 건다. 다른 TTS는 태그를 글자로 읽는다
    tts_style: bool = False   # DJ의 에너지로 정한 스타일 문장을 합성할 때 함께 보낸다
    tts_pauses: bool = False  # 문장 사이에 쉼 태그를 넣는다
    tts_vocal_tags: bool = False  # 대본을 쓰는 LLM이 웃을 자리에 <laugh>·<chuckle>을 넣게 한다 (멘트에 하나까지)
    tts_moods: dict = field(default_factory=dict)  # 에너지별 문구를 바꿀 때 — {"low": "...", "mid": "...", "high": "..."}
    max_concurrent_generations: int = 2
    # 백엔드가 track.queued를 보내기 전에 더미 곡 이름으로 곡 소개를 만들지. 로컬 실행용이다 —
    # 운영(--serve)에서는 끈다: 실제로 나오지 않는 곡을 소개하게 된다
    dummy_tracks: bool = True
    target_buffer_sec: float = 30.0
    # 켜면 코너 진입을 백엔드에 알린다. 지금 백엔드는 모르는 이벤트를 격리하므로 #170 전까지 끈다
    emit_corner_changed: bool = False
    # 켜면 corner.changed에 형식(format)을 싣는다 — 방송 중 형식 전환 때 화면이 새 형식의 첫 멘트에 맞춰 바뀐다 (#260).
    # 백엔드가 이 필드를 받게 배포된 뒤에 켠다: 그 전에는 모르는 필드라 이벤트째 격리된다
    emit_corner_format: bool = False
    # 켜면 DJ 대 청취자 코너의 투표를 연다 (vote.open, 계약 10.8절, #279). 백엔드가 vote.open을 받고
    # vote.result를 돌려주게 배포된 뒤에 켠다 — 꺼져 있으면 그 코너는 지금처럼 주제 토크로 대체된다
    emit_votes: bool = False
    # DJ 선곡 (계약 3.4절 track.suggest, #295) — 들고 있는 다음 곡이 없으면 DJ가 곡 후보를 내고 백엔드가 찾아 튼다.
    # 지금 백엔드는 모르는 이벤트를 격리하므로, 백엔드가 track.suggest를 받게 배포된 뒤에 켠다
    emit_song_picks: bool = False
    song_fit_timeout_sec: float = 4.0  # 신청곡 판정(REST)을 기다리는 상한 — 넘으면 통과로 답한다 [예시]
    song_pick_grace_sec: float = 5.0  # 백엔드가 다음 곡을 알려 줄 시점부터 이만큼 기다린 뒤에 제안한다 [예시]
    # 제안에 답이 없으면 이만큼 뒤에 다시 제안한다. 백엔드는 검색과 음원 받기에 최대 240초를 쓰고 그 안에
    # 반드시 답한다(apps/backend/app/dj_picks.py TOTAL_BUDGET_SEC) — 그보다 길어야 같은 자리에 두 번 제안하지 않는다
    song_pick_timeout_sec: float = 300.0
    vote_window_sec: float = 60.0  # 투표 창 길이 — 기준 세그먼트의 재생 시작부터 [예시]
    # 이야깃거리 데이터셋 (#332, CORNER_DESIGN 5.9절) — 방송 시작 때 주제와 가까운 이야깃거리를 찾아 갈래 계획의
    # 참고 자료로 넣는다. None이면 쓰지 않고 LLM만으로 갈래를 짠다
    talk_bank_dir: Path | None = None
    talk_bank_cache: Path = field(default_factory=lambda: Path("var/talk_bank.sqlite"))  # 벡터 캐시 — 지워도 된다
    embedder: str = "dummy"  # dummy(글자 겹침, 키 불필요) | gemini(GEMINI_API_KEY 필요)
    # 갈래마다 '하려는 말'을 붙이고 대본이 그 말을 풀어 가게 한다 (inputs/topic_plan.py). 0이면 붙이지 않는다.
    # 1: 하려는 말 + DJ의 취향·의견  2: 1 + 공감을 바라는 주제는 마음을 알아주는 말로, 비유 없이 평범한 말로
    # 3: 2의 내용에, 지시를 줄이고 페르소나를 앞세워 쓴다 — 말하는 방식은 DJ의 에너지·농담 정도가 정한다 (#376)
    talk_gist: int = 0
    # 방송별로 쓴 갈래의 기록 — 기간 안에 다른 방송이 쓴 갈래를 규칙으로 뺀다. None이면 적지도 거르지도 않는다
    topic_usage_path: Path | None = None
    topic_reuse_days: float = TOPIC_REUSE_DAYS
    # 뉴스 피드 읽기를 바꿔 끼운다 (URL → 바이트). 시나리오 실행기와 테스트가 네트워크 없이 돌릴 때 쓴다
    rss_fetch: Callable[[str], bytes] | None = None
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


# 방송 하나에 만들어 두는 투표 고민 수 — 코너 한 번에 세 개를 쓴다. 두 번째 진입에는 남은 것만 쓴다 [예시]
_DILEMMA_COUNT = 5


class StationRejected(Exception):
    """방송을 시작할 수 없다 — station.rejected로 백엔드에 돌려보낸다 (계약 3.1절)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class FormatChanged(BaseModel):
    """station.format_changed 페이로드 (계약 3.2절) — 필드 뜻은 station.created와 같다."""

    format: Literal["talk", "music", "news"]
    corners: list[str] | None = None  # talk: 본 코너. 없으면 형식 기본값
    news_sections: list[str] | None = None  # news: 분야. 없으면 형식 기본값


class PersonaChanged(BaseModel):
    """station.persona_changed 페이로드 (계약 3.2절) — persona는 station.created의 것과 같은 구조다."""

    persona: Persona


_PICK_POLL_SEC = 1.0
_PICK_RETRY_SEC = 20.0  # 곡을 내지 못했을 때 다시 고르기까지 [예시]
_PICK_TAKEN_WAIT_SEC = 60.0  # 소개한 곡의 시작 통보를 기다리는 상한 [예시]
_PICK_NOT_NEEDED_WAIT_SEC = 60.0  # 백엔드가 "틀 곡이 있다"고 답했을 때 [예시]
_PICK_MAX_FAILURES = 3  # 연달아 이만큼 실패하면 쉰다 [예시]
_PICK_REST_SEC = 120.0


def _song_label(track: dict) -> str:
    return " — ".join(str(track[k]) for k in ("title", "artist") if track.get(k))


def profile_from(persona: Persona) -> StationProfile:
    """persona의 표현 층만 프롬프트용 프로필로 옮긴다 (docs/persona.md 2절)."""
    return StationProfile(
        dj_name=persona.dj_name, tone=persona.tone, concept=persona.concept,
        examples=list(persona.examples), forbidden=list(persona.forbidden),
        signature_phrases=list(persona.signature_phrases), energy=persona.style.energy,
        humor=persona.style.humor,
    )


class StationEngine:
    def __init__(self, config: StationConfig, settings: EngineSettings, transport: Transport):
        self.config = config
        self.settings = settings
        self.transport = transport
        # 주제는 방송 시작 때 정해진다 — 호스트가 비워 컨셉을 썼어도 방송 중 페르소나 전환(#261)으로 바뀌지 않는다
        self._topic = config.topic or config.profile.concept
        # 보이스마다 TTS 클라이언트를 만든다. 시나리오 실행기는 흉내 TTS로 바꿔 끼운다
        self.tts_factory: Callable[[str | None], TtsClient] = (
            lambda voice: make_tts(settings.tts, voice=voice or settings.tts_voice))

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
            tts_client = self._tts_client(config.voice, config.profile)
        except RuntimeError as exc:
            raise StationRejected("tts_unavailable", str(exc)) from exc
        try:
            llm = make_llm(settings.llm, model=settings.llm_model,
                           base_url=settings.llm_base_url,
                           reasoning_effort=settings.llm_reasoning_effort)
        except RuntimeError as exc:
            raise StationRejected("llm_unavailable", str(exc)) from exc

        self.telemetry = Telemetry(settings.sqlite_path, config.station_id)
        self._talk_gist = settings.talk_gist
        self.telemetry.corner_titles = {"filler": "대체 멘트",
                                        **{name: spec.title for name, spec in fmt.corners.items()}}
        if config.snapshot is not None:
            self.telemetry.log_station("station.created", config.snapshot)
        # 제출 페이로드의 audio_ref는 이 루트 기준 상대 경로로 나간다 (설계 문서 5.1)
        audio_root = Path(settings.audio_dir)
        # 파이프라인과 접착 요소 모두 원자적 쓰기·캐시·길이 실측을 거친다
        tts = CachedTts(tts_client, cache_dir=settings.tts_cache_dir, pause_ms=settings.pause_ms,
                        split_chars=settings.tts_split_chars)
        safety = SafetyChecker(settings.safety_rules_path)
        pipeline = GenerationPipeline(
            station_id=config.station_id, profile=config.profile,
            llm=llm, tts=tts, safety=safety,
            telemetry=self.telemetry, audio_root=audio_root,
            recent_segments=config.recent_segments, hedge=settings.hedge,
        )
        self.tracks = TrackSource(Catalog(), dummy=settings.dummy_tracks)
        self._safety = safety
        self.rss = self._make_rss(fmt, config.news_sections, news_feeds)
        self.sources = MaterialSources(
            tracks=self.tracks, rss=self.rss,
            inbox=MessageInbox(max_chars=chat_rules.message_max_chars, violates=safety.check_l2),
            stories=StoryPool(topic_words=keywords(self.topic), violates=safety.check_l2, judged=True),
        )
        self.story_judge = StoryJudge(llm, self.topic, self.sources.stories, telemetry=self.telemetry)
        self._llm = llm
        # 이 방송의 id — 방송별 사용 기록에서 "다른 방송"을 가린다. 스테이션 id는 고정 채널이라 쓸 수 없다 (#332)
        self._broadcast_id = new_id("bc")
        self._bank, self._usage = self._make_talk_sources()
        self.topic_plan = self._make_topic_plan(fmt)
        self.sources.votes.window_sec = settings.vote_window_sec
        self._dilemma_task: asyncio.Task | None = None  # 투표 고민 만들기 — 방송에 한 번
        # DJ 선곡 — 설정이 꺼져 있으면 고르지 않는다
        self.song_picker = SongPicker(llm, config.profile, config.music_taste, self.topic,
                                      violates=safety.check_l2) if settings.emit_song_picks else None
        # 신청곡이 방의 장르·분위기에 어울리는지 — 백엔드가 음원을 받기 전에 REST로 묻는다 (계약 6.4절)
        self.song_fit = SongFitJudge(llm, config.profile, config.music_taste, self.topic,
                                     violates=safety.check_l0, telemetry=self.telemetry)
        self._pick_task: asyncio.Task | None = None
        self._pending_pick: tuple[str, list[Pick], float] | None = None  # 답을 기다리는 제안 (id, 후보, 보낸 때)
        self._pick_after = 0.0  # 이 시각 전에는 제안하지 않는다
        self._pick_failures = 0  # 연달아 실패한 횟수
        self._played: list[str] = []  # 이번 방송에서 튼 곡 — 다시 고르지 않는다
        if config.first_song and config.first_song.get("title"):
            self._played.append(_song_label(config.first_song))
        self._dilemmas_planned = False
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
            emit_corner_format=settings.emit_corner_format,
            tone_judge=(ChatToneJudge(llm, self.topic, telemetry=self.telemetry)
                        if chat_rules.tone_judge else None),
        )
        self._contract_version = settings.contract_version
        # 방송 중 전환(#260, #261)이 받아들인 형식과 DJ — 편성 루프가 바꾸기 전이어도 다음 전환은 이 값을 쓴다.
        # 형식과 페르소나를 잇달아 바꿀 때 연결 멘트가 둘 다 반영하게 한다
        self._fmt = fmt
        self._profile = config.profile
        self._tts = tts
        self._poller: asyncio.Task | None = None  # 뉴스 피드 폴링
        self._plan_task: asyncio.Task | None = None  # 이야기 갈래 계획
        self._audio_root = audio_root
        self._glue_no = 0  # 방송 중에 연결 멘트를 다시 합성한 횟수 — 폴더 이름에 쓴다
        self._glue_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def topic(self) -> str:
        """이 방송의 확정된 주제 — station.started로 백엔드에 알린다 (백엔드는 이 주제로 사연 후보를 고른다).

        TODO(오프닝 코너): 호스트가 비웠으면 컨셉에서 LLM으로 만든다. 지금은 컨셉을 그대로 쓴다.
        """
        return self._topic

    def _make_rss(self, fmt, sections: list[str] | None, news_feeds) -> RssCollector:
        """뉴스 형식의 소재 수집기. 뉴스가 아니면(news_feeds 없음) 피드 없는 빈 수집기다."""
        if news_feeds is None:
            return RssCollector()
        # 호스트가 고른 분야만 폴링한다. section은 코너의 label(분야 이름)과 맞춘다
        return RssCollector(
            feeds=[Feed(entry.source, entry.url, fmt.section_names.get(code, code))
                   for code in sections or fmt.default_sections
                   for entry in news_feeds.feeds.get(code, [])],
            poll_sec=news_feeds.poll_sec, max_age_sec=news_feeds.max_age_hours * 3600,
            violates=self._safety.check_l0, fetch=self.settings.rss_fetch,
            on_refresh=self.telemetry.log_rss,
        )

    def _make_talk_sources(self) -> tuple[TalkBank | None, TopicUsage | None]:
        """이야깃거리 데이터셋과 방송별 사용 기록 (#332). 열지 못해도 방송은 한다 — 갈래를 LLM만으로 짠다."""
        s = self.settings
        if s.talk_bank_dir is None and s.topic_usage_path is None:
            return None, None
        try:
            embedder = make_embedder(s.embedder)
            bank = shared_bank(s.talk_bank_dir, embedder, s.talk_bank_cache) if s.talk_bank_dir else None
            usage = (TopicUsage(s.topic_usage_path, embedder, days=s.topic_reuse_days)
                     if s.topic_usage_path else None)
        except (RuntimeError, ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:  # 키 누락·파일 오류
            logger.warning("TALK_BANK_OFF %s: %s", self.config.station_id, exc)
            return None, None
        return bank, usage

    def _make_topic_plan(self, fmt) -> TopicPlan | None:
        """호스트가 정한 주제가 있고 이 형식에 주제 토크가 있을 때만 — 컨셉을 갈래로 펼치지는 않는다."""
        talks = any({s.kind, s.again} & {"talk_open", "talk_resume"}
                    for c in fmt.corners.values() for s in c.steps)
        if not (self.config.topic and talks):
            return None
        return TopicPlan(
            self._llm, self.config.topic, count=angle_count(self.config.broadcast_minutes),
            violates=self._safety.check_l2, forbidden=self.config.profile.forbidden,
            telemetry=self.telemetry, bank=self._bank, usage=self._usage,
            station_id=self.config.station_id, broadcast_id=self._broadcast_id, fmt=fmt.format,
            persona=persona_text(self.config.profile.concept, self.config.profile.tone),
            gist_level=self._talk_gist,
        )

    def _tts_client(self, voice: str | None, profile: StationProfile) -> TtsClient:
        """이 DJ의 TTS 클라이언트 — 말투 지시와 쉼 태그를 받을 수 있는 TTS면 DJ의 에너지로 정해 건다 (#369).

        말투는 DJ마다 한 문장으로 고정한다. 멘트마다 바꾸면 목소리가 흔들린다(공식 가이드, persona.md 9절).
        """
        client = self.tts_factory(voice)
        use_style = getattr(client, "use_style", None)
        if use_style is not None and (self.settings.tts_style or self.settings.tts_pauses):
            style = voice_style(profile.energy, self.settings.tts_moods) if self.settings.tts_style else None
            use_style(style, pauses=self.settings.tts_pauses)
        # 발성 태그는 받는 TTS에서만 쓰게 한다 — 대본 프롬프트가 이 값을 본다 (corners/base.py)
        profile.vocal_tags = self.settings.tts_vocal_tags and bool(getattr(client, "vocal_tags", False))
        profile.voice_first = self.settings.talk_gist >= 3  # 방송 시작과 페르소나 전환 때 함께 정한다 (#376)
        return client

    def _plan_dilemmas(self, fmt, corners: list[str] | None) -> None:
        """본 코너에 투표 코너가 있으면 고민을 뒤에서 만든다 (#279). 방송에 한 번만 만든다.

        끝나기 전에 그 코너의 차례가 오면 고민이 없어 주제 토크로 대체되고, 다음 차례부터 열린다.
        """
        mains = corners or fmt.default_main or fmt.host_corners
        if (not self.settings.emit_votes or self._dilemmas_planned
                or not any(fmt.corner(name).requires.get("vote") for name in mains)):
            return
        self._dilemmas_planned = True
        plan = DilemmaPlan(self._llm, self.topic, self._profile, count=_DILEMMA_COUNT,
                           violates=self._safety.check_l2, on_ready=self.sources.votes.add,
                           telemetry=self.telemetry)
        self._dilemma_task = asyncio.create_task(plan.run())

    async def run(self, max_segments: int | None = None) -> None:
        # 수집은 사전 렌더링과 동시에 시작한다 — 방송 시작을 피드 시간만큼 더 늦추지 않는다
        poller = self._poller = asyncio.create_task(self.rss.run()) if self.rss.feeds else None
        # 갈래 계획도 기다리지 않는다 — 오프닝과 첫 곡이 나가는 동안 끝나면 첫 주제 토크부터 쓴다
        self._plan_task = asyncio.create_task(self.topic_plan.run()) if self.topic_plan else None
        self._plan_dilemmas(self._fmt, self.config.corners)
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
            if self.song_picker is not None:
                self._pick_after = time.time() + self.settings.song_pick_grace_sec
                self._pick_task = asyncio.create_task(self._pick_songs())
            await self.scheduler.run(self._stop, max_segments=max_segments)
        finally:
            # 방송 중 형식 전환으로 폴링과 갈래 계획이 바뀌었을 수 있다 — 지금 것을 멈춘다
            tasks = [t for t in (self._poller, self._plan_task, judge, self._glue_task, self._dilemma_task,
                                 self._pick_task) if t is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.telemetry.close()
            if self._usage is not None:
                self._usage.close()

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
        if payload.get("source") == "dj":
            track["dj_pick"] = True
        if self.song_picker is not None:
            self._note_queued(track, payload)
        self.tracks.queue(track)
        self.telemetry.log_track(track)

    def _note_queued(self, track: dict, payload: dict) -> None:
        """다음 곡이 정해졌다 — 튼 곡으로 적어 두고, DJ가 제안한 곡이면 고른 까닭을 붙인다.

        소개는 track의 실제 곡 정보로 한다. 제안에서는 고른 까닭을 가져오고, 찾은 곡이 제안한 곡으로 확인되면
        제목과 가수도 제안한 것으로 바꾼다 — 영상을 올린 채널 이름이 가수로 오는 경우가 있다 (#385).
        """
        self._played.append(_song_label(track))
        # 백엔드가 다음 곡을 알려 왔다 — 이 곡이 나갈 때까지는 고를 일이 없다
        self._pick_after = max(self._pick_after, time.time() + self.settings.song_pick_grace_sec)
        pending = self._pending_pick
        if payload.get("source") != "dj" or pending is None or payload.get("suggest_id") != pending[0]:
            return
        self._pending_pick = None
        self._pick_failures = 0
        index = payload.get("candidate")
        known = isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(pending[1])
        if not known:
            return
        pick = pending[1][index]
        if pick.why:
            track["pick_why"] = pick.why
        if (names := confirmed_names(pick, track)) and names != (track.get("title"), track.get("artist")):
            track["found_title"], track["found_artist"] = track.get("title"), track.get("artist")
            track["title"], track["artist"] = names
            self._played[-1] = _song_label(track)

    def on_track_suggest_failed(self, payload: dict) -> None:
        """백엔드가 제안한 곡을 넣지 못했다 (계약 3.4절) — 다른 후보로 다시 제안한다. 연달아 실패하면 쉰다."""
        suggest_id, reason = payload.get("suggest_id"), str(payload.get("reason") or "unknown")[:40]
        if self._pending_pick is None or suggest_id != self._pending_pick[0]:
            return
        self._pending_pick = None
        self.telemetry.log_song_pick_failed(suggest_id, reason)
        now = time.time()
        if reason == "not_needed":  # 백엔드에 틀 곡이 있다 — 곧 track.queued로 알려 온다
            self._pick_after = now + _PICK_NOT_NEEDED_WAIT_SEC
            return
        self._pick_failures += 1
        if self._pick_failures >= _PICK_MAX_FAILURES:
            self._pick_failures = 0
            self._pick_after = now + _PICK_REST_SEC
        else:
            self._pick_after = now + self.settings.song_pick_grace_sec

    def _needs_pick(self, now: float) -> bool:
        """지금 곡을 골라야 한다 — 들고 있는 다음 곡이 없고, 백엔드가 알려 줄 때를 기다릴 만큼 기다렸다."""
        if self.tracks.has_queued or now < self._pick_after:
            return False
        # 소개한 곡이 시작되면 백엔드가 그다음 곡을 알린다 — 그때까지 기다린다. 시작 통보가 오지 않으면 기다리지 않는다
        taken = self.tracks.taken_at
        if taken is not None and now - taken < _PICK_TAKEN_WAIT_SEC:
            return False
        if self._pending_pick is not None:
            if now - self._pending_pick[2] < self.settings.song_pick_timeout_sec:
                return False
            self.telemetry.log_song_pick_failed(self._pending_pick[0], "timeout")
            self._pending_pick = None
        return True

    async def _pick_songs(self) -> None:
        """들고 있는 다음 곡이 없으면 DJ가 곡 후보를 내 백엔드에 알린다 (계약 3.4절 track.suggest, #295)."""
        assert self.song_picker is not None
        while True:
            await asyncio.sleep(_PICK_POLL_SEC)
            if not self._needs_pick(time.time()):
                continue
            picks = await self.song_picker.pick(list(self._played))
            now = time.time()
            if not picks:  # 제공자 장애이거나 새 곡을 내지 못했다 — 조금 뒤에 다시 고른다
                self._pick_after = now + _PICK_RETRY_SEC
                continue
            if self.tracks.has_queued:  # 고르는 사이 백엔드가 곡을 알려 왔다 — 이번 후보는 다음에 쓴다
                continue
            suggest_id = new_id("sg")
            self._pending_pick = (suggest_id, picks, now)
            await self.transport.notify_station("track.suggest", {
                "suggest_id": suggest_id,
                "candidates": [{"title": pick.title, "artist": pick.artist} for pick in picks],
            })
            self.telemetry.log_song_pick(suggest_id, picks)

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

    async def on_persona_changed(self, payload: dict,
                                 voice_known: Callable[[str], Awaitable[bool]] | None = None) -> bool:
        """운영자가 고른 페르소나로 바꾼다 (계약 3.2절 station.persona_changed). 받아들였으면 True.

        방 생성 때와 같은 기준(형식, 보이스 목록, 안전 규칙)으로 검사한다. 걸리면 바꾸지 않고 기록만 남긴다 —
        이전 페르소나로 계속 방송한다. 대본과 목소리는 편성 루프의 다음 차례에 바뀌고, 연결 멘트는 그동안
        뒤에서 새 목소리로 다시 합성한다(끝날 때까지 연결 멘트 없이 코너에 들어간다).
        """
        try:
            persona = PersonaChanged.model_validate(payload).persona
        except ValidationError as exc:
            return self._reject_persona("invalid_payload", error_detail(exc))
        if persona.persona_id is None:  # station.created와 같다 — 백엔드가 발급한 값으로 전환을 구분한다
            return self._reject_persona("invalid_payload", "persona.persona_id: Field required")
        if persona.persona_id in (self.config.persona_id, self.scheduler.pending_persona_id):
            return False  # at-least-once 재전달 — 이미 바꿨거나 바꾸는 중이다
        try:
            if voice_known is not None and not await voice_known(persona.voice):
                return self._reject_persona(
                    "unknown_voice", f"persona.voice: {persona.voice} — {self.settings.tts} 보이스 목록에 없음")
        except StationRejected as exc:  # 보이스 목록 조회 실패
            return self._reject_persona(exc.reason, exc.detail)
        if unsafe := unsafe_fields(persona.model_dump(), self._safety):
            name, rule = unsafe[0]
            return self._reject_persona("unsafe_persona", f"persona.{name}: 안전 규칙에 걸렸습니다 ({rule})")
        profile = profile_from(persona)
        try:
            tts = CachedTts(self._tts_client(persona.voice, profile), cache_dir=self.settings.tts_cache_dir,
                            pause_ms=self.settings.pause_ms, split_chars=self.settings.tts_split_chars)
        except RuntimeError as exc:
            return self._reject_persona("tts_unavailable", str(exc)[:300])
        glue = self._new_glue(self._fmt, profile, tts)
        try:
            glue.check()
        except ValueError as exc:
            return self._reject_persona("unsafe_persona", str(exc)[:300])
        self._profile, self._tts = profile, tts
        genres = [str(genre) for genre in persona.music_taste]
        self.song_fit.use_persona(profile, genres)
        if self.song_picker is not None:
            self.song_picker.use_persona(profile, genres)
        self._use_glue(glue)
        self.scheduler.switch_persona(PersonaSwitch(
            profile=profile, tts=tts, persona_id=persona.persona_id, voice=persona.voice,
            snapshot=persona.model_dump(mode="json")))
        return True

    def on_format_changed(self, payload: dict) -> bool:
        """운영자가 고른 형식으로 바꾼다 (계약 3.2절 station.format_changed). 받아들였으면 True.

        방 생성 때와 같은 기준(형식 파일, 코너, 분야)으로 검사한다. 걸리면 바꾸지 않고 기록만 남긴다.
        클락과 코너는 편성 루프의 다음 차례에 바뀐다 — 오프닝을 다시 하지 않고 새 형식의 순환으로 들어간다.
        뉴스로 바꾸면 여기서 기사 수집을 시작하고, 연결 멘트는 새 형식의 문구로 뒤에서 다시 합성한다.
        """
        try:
            change = FormatChanged.model_validate(payload)
        except ValidationError as exc:
            return self._reject_format("invalid_payload", error_detail(exc))
        corners = change.corners if change.format == "talk" else None
        sections = change.news_sections if change.format == "news" else None
        if (change.format, corners, sections) == self.scheduler.upcoming_format:
            return False  # at-least-once 재전달 — 이미 그 형식이다
        try:
            fmt = load_format(change.format, self.settings.formats_dir)
            clock = Clock(fmt, main_corners=corners, sections=sections, skip_opening=True)
            unknown = [code for code in sections or [] if code not in fmt.section_names]
            if unknown:
                raise ValueError(f"{fmt.format} 형식에 없는 분야: {unknown}")
            news_feeds = load_news_feeds(self.settings.news_feeds_path) if fmt.format == "news" else None
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return self._reject_format("invalid_payload", f"형식 설정: {exc}"[:300])
        glue = self._new_glue(fmt, self._profile, self._tts)
        try:
            glue.check()
        except ValueError as exc:
            return self._reject_format("invalid_payload", str(exc)[:300])
        rss = self._make_rss(fmt, sections, news_feeds)
        if self._poller is not None:  # 이전 형식의 피드는 더 읽지 않는다
            self._poller.cancel()
        self._poller = asyncio.create_task(rss.run()) if rss.feeds else None
        self.rss = rss
        plan = None
        if self.topic_plan is None and (plan := self._make_topic_plan(fmt)) is not None:
            # 주제 토크가 없는 형식으로 시작한 방송 — 갈래 계획을 지금 세운다. 끝나기 전에는 주제 한 줄로 말한다
            self.topic_plan = plan
            self._plan_task = asyncio.create_task(plan.run())
        self._fmt = fmt
        self._plan_dilemmas(fmt, corners)
        self._use_glue(glue)
        self.scheduler.switch_format(FormatSwitch(
            fmt=fmt, clock=clock, intensity=fmt.intensity[self.config.intensity], rss=rss,
            corners=corners, news_sections=sections, topic_plan=plan,
            ready=rss.ready if rss.feeds else None))
        return True

    def on_vote_result(self, payload: dict) -> None:
        """투표 창이 닫혔다 (계약 10.8절 vote.result) — 결과 공개 멘트의 소재가 된다."""
        vote_id, counts = payload.get("vote_id"), payload.get("counts")
        if (not isinstance(vote_id, str) or not isinstance(counts, dict)
                or not all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
                           for k, v in counts.items())):
            raise ValueError(f"vote.result 형식 오류: {payload!r}"[:200])
        self.scheduler.on_vote_result(vote_id, counts)

    def _reject_format(self, reason: str, detail: str) -> bool:
        logger.warning("FORMAT_REJECTED %s %s: %s", self.config.station_id, reason, detail)
        self.telemetry.log_format_rejected(reason, detail)
        return False

    def _new_glue(self, fmt, profile: StationProfile, tts: TtsClient) -> GlueBank:
        """방송 중 전환 뒤에 쓸 연결 멘트. 이전 문구 파일은 아직 재생 전일 수 있어 새 폴더에 합성한다."""
        self._glue_no += 1
        return GlueBank(tts=tts, audio_root=self._audio_root, station_id=self.config.station_id,
                        profile=profile, fmt=fmt, safety=self._safety, subdir=f"glue_{self._glue_no}")

    def _use_glue(self, glue: GlueBank) -> None:
        """뒤에서 합성을 시작하고 편성 관리자에 넘긴다 — 전환이 적용될 때부터 쓰인다."""
        if self._glue_task is not None:  # 앞선 전환의 합성 — 이제 쓰지 않는다
            self._glue_task.cancel()
        self._glue_task = asyncio.create_task(self._rerender_glue(glue))
        self.glue = glue
        self.scheduler.use_glue(glue)

    def _reject_persona(self, reason: str, detail: str) -> bool:
        logger.warning("PERSONA_REJECTED %s %s: %s", self.config.station_id, reason, detail)
        self.telemetry.log_persona_rejected(reason, detail)
        return False

    async def _rerender_glue(self, glue: GlueBank) -> None:
        """전환 뒤의 연결 멘트 합성. 실패해도 방송은 이어진다 — 합성된 문구만 쓰고 나머지 코너는 문구 없이 들어간다."""
        if not glue.texts():  # 뉴스·음악처럼 연결 멘트가 없는 형식 — 합성할 것도 남길 기록도 없다
            return
        started = time.time()
        try:
            await glue.prerender()
        except Exception as exc:
            logger.exception("GLUE_RERENDER_FAILED %s", self.config.station_id)
            self.telemetry.log_glue(len(glue), time.time() - started, error=str(exc)[:100] or type(exc).__name__)
        else:
            self.telemetry.log_glue(len(glue), time.time() - started)

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
        # 곡이 시작되면 백엔드가 그다음 곡을 알린다 — 그만큼 기다렸다가 없으면 DJ가 고른다
        self.tracks.started()
        if self.song_picker is not None:
            self._pick_after = max(self._pick_after, time.time() + self.settings.song_pick_grace_sec)

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
