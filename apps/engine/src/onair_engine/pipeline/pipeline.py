"""생성 파이프라인 — 프롬프트 → LLM(L1) → 인용 검사 → L2 → TTS → 패키징 (설계 문서 4.4).

모든 단계 경계에서 타임스탬프를 기록한다. 안전 검사는 전부 제출 이전에 끝난다.
실패 처리(MVP 규칙, 설계 문서 4.4): LLM/TTS 호출은 1회 재시도하고, 재실패하면
더미 LLM의 고정 filler 대본으로 대체한다 — 방송은 제공자 장애로 멈추지 않는다.
"""
from __future__ import annotations

import itertools
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from ..corners.steps import build_prompt
from ..domain import (
    GenerationJob,
    Material,
    Prompt,
    Script,
    SegmentKind,
    SegmentSubmission,
    StationProfile,
    new_id,
    to_audio_ref,
)
from ..prompt_data import RECENT, data_block
from ..telemetry import Telemetry
from .llm import REJECT, DummyLlmClient, LlmClient
from .safety import SafetyChecker
from .tts import TtsClient, TtsQuotaError

_ATTEMPTS = 2  # 최초 1회 + 재시도 1회
_PENDING_AIR_MAX = 256
# 인용 검사 — 채팅 글자 bigram 중 이만큼이 대본에 있으면 인용한 것으로 본다 [예시].
# LLM은 "컵라면이요"를 "컵라면이라고"처럼 어미를 바꿔 인용하므로 문자열 그대로는 비교하지 않는다
QUOTE_MATCH = 0.5
# 풀어서 한 인용 — 청취자가 한 말이라고 밝혔으면("~냐고 물어보신 분이 있네요") 일치 비율이 이만큼만 돼도
# 인용으로 본다. 2026-11-12 평가(채팅 162건)에서 누락 58건 중 43건이 이런 대본이었고, 그 일치 비율은
# 0.25~0.46에 몰려 있었다. 0.2 아래는 채팅의 낱말이 거의 남지 않은 대본이다 (ENGINE_TUNING_LOG T-006) [실험 T-006]
PARAPHRASE_MATCH = 0.2
# 청취자가 한 말이라고 밝히는 표현. "~하신 분이 있네요", "~라는 분도 계시네요", "~냐고 물어보신"
_ATTRIBUTION = re.compile(r"(분|사람)(들)?(이|도|께서)?\s?(있|계시|계신|많)|물어보|물어본|물으신|물으셨")
# DJ를 부르는 말 — DJ는 이 부분을 되읽지 않으므로 풀어서 한 인용을 볼 때는 비교에서 뺀다
# ("DJ님은 뭐 드셨어요?" → "뭐 먹었냐고 물어보신 분이 있네요")
_ADDRESS = re.compile(r"(DJ|디제이)\s?님?(은|는|이|가|도|께|께서)?[,\s]*", re.IGNORECASE)


def quote_match(chat: str, script: str, *, without_address: bool = False) -> float:
    """채팅의 글자 두 개 묶음 가운데 대본에도 있는 비율 (0~1). without_address면 DJ를 부르는 말을 빼고 센다."""
    def bigrams(text: str) -> set[str]:
        chars = [c for c in text if c.isalnum()]
        return {a + b for a, b in itertools.pairwise(chars)}

    want = (bigrams(_ADDRESS.sub("", chat)) if without_address else set()) or bigrams(chat)
    if not want:  # '!!'나 한 글자처럼 묶을 글자가 없는 채팅 — 비교할 것이 없으니 인용된 것으로 본다
        return 1.0
    return len(want & bigrams(script)) / len(want)


def attributed(script: str) -> bool:
    """대본이 청취자가 한 말이라고 밝히는가."""
    return _ATTRIBUTION.search(script) is not None


def quoted(chat: str, script: str) -> bool:
    """대본에 채팅이 인용됐는가 (CORNER_DESIGN 5.1절 — 넣었으면 반드시 인용한다, 빠졌으면 되돌린다).

    글자가 절반 넘게 겹치거나, 청취자의 말이라고 밝히면서 풀어 말했으면 인용이다. 답만 하고
    누가 물었는지 말하지 않은 대본은 인용이 아니다 — 청취자가 자기 채팅인 줄 알 수 없다.
    """
    if quote_match(chat, script) >= QUOTE_MATCH:
        return True
    return attributed(script) and quote_match(chat, script, without_address=True) >= PARAPHRASE_MATCH

T = TypeVar("T")

logger = logging.getLogger(__name__)


class GenerationPipeline:
    def __init__(self, *, station_id: str, profile: StationProfile,
                 llm: LlmClient, tts: TtsClient, safety: SafetyChecker,
                 telemetry: Telemetry, audio_root: Path, recent_segments: int = 8):
        self.station_id = station_id
        self.profile = profile
        self.llm = llm
        self.tts = tts
        self.safety = safety
        self.telemetry = telemetry
        # audio_root는 백엔드와 공유하는 오디오 루트, audio_dir은 이 스테이션의 하위 디렉토리
        self.audio_root = Path(audio_root)
        self.audio_dir = self.audio_root / station_id
        # 제공자 장애 시 대체 대본 — 네트워크 없이 즉시 나온다
        self._fallback_llm = DummyLlmClient(delay_sec=0)
        # 방송 맥락 = 최근 N개 대본 (확인 10). 실 LLM이 매번 같은 인사로 시작하지 않게 한다
        self._recent: deque[str] = deque(maxlen=recent_segments)
        # 생성했지만 아직 송출 확인이 안 된 대본 (context-memory 3.1절 pending_air)
        self._pending_air: dict[str, str] = {}

    def take_aired(self, segment_id: str) -> str | None:
        """PUBLISHED를 받은 세그먼트의 대본. 재전달이나 모르는 세그먼트면 None."""
        return self._pending_air.pop(segment_id, None)

    @property
    def tts_blocked_until(self) -> float:
        """TTS 한도 초과로 호출하지 않는 시각까지. 그동안 생성해도 합성할 수 없다."""
        return getattr(self.tts, "blocked_until", 0.0)

    async def run(self, job: GenerationJob) -> SegmentSubmission | None:
        """성공 시 제출 페이로드, REJECT 시 None.

        request_ref는 인용한 청취자 입력이다 — 넣은 채팅이 대본에 실제로 들어갔을 때만 채운다.
        비어 있으면 스케줄러가 그 입력을 되돌린다. LLM/TTS가 재시도까지 실패하면 filler 대체
        세그먼트를 돌려준다(곡 소개였다면 music_ref는 유지한다 — 곡 재생이 멘트 실패에 막히지 않게).
        """
        # [1] 프롬프트 구성 (L1 지시 내장 + 방송 맥락)
        t0 = time.time()
        prompt = self._with_context(
            build_prompt(job.step, job.material, self.profile, plain=job.plain))

        # [2] LLM 호출
        try:
            script = await self._retry(job, "llm", lambda: self.llm.generate(prompt))
        except Exception:
            logger.exception("LLM_FAILED %s %s", job.job_id, job.corner_type)
            return await self._fallback(job)
        t1 = time.time()
        job.script_chars = len(script.text)
        self._log(job, "llm", t0, t1, "ok")
        if script.text.strip() == REJECT:
            self._log(job, "l1", t1, t1, "reject")
            return None

        # [3] 인용 검사 — 빠진 입력은 request_ref를 비워 스케줄러가 되돌리게 한다
        quote_ref = None
        if job.material.quotes:
            first = job.material.quotes[0]
            ok = self._check_quote(job, "chat", first.body, script.text, t1)
            quote_ref = first.request_id if ok else None
        elif job.material.feature_ref:
            # 낭독은 대본에 글이 들어갔는지 본다. 사연·신청곡은 DJ가 풀어 말하므로 비교하지 않는다
            ok = (job.material.must_read is None
                  or self._check_quote(job, "read", job.material.must_read, script.text, t1))
            quote_ref = job.material.feature_ref if ok else None

        try:
            return await self._finish(job, script, t1, quote_ref)
        except _TtsFailed:
            return await self._fallback(job)

    async def _finish(self, job: GenerationJob, script: Script, started: float,
                      quote_ref: str | None = None) -> SegmentSubmission | None:
        # [4] L2 출력단 검사
        violation = self.safety.check_l2(script.text)
        t2 = time.time()
        self._log(job, "l2", started, t2, "reject" if violation else "ok")
        if violation:
            return None

        # [5] TTS
        seg_id = new_id("seg")
        logger.info("SCRIPT %s [%s] %s", seg_id, job.corner_type, script.text)
        out_path = self.audio_dir / f"{seg_id}{self.tts.file_ext}"
        try:
            duration_ms = await self._retry(
                job, "tts", lambda: self.tts.synthesize(script.text, out_path))
        except Exception as exc:
            logger.exception("TTS_FAILED %s %s", job.job_id, job.corner_type)
            raise _TtsFailed from exc
        t3 = time.time()
        self._log(job, "tts", t2, t3, "ok")
        self._recent.append(script.text)
        self._pending_air[seg_id] = script.text
        while len(self._pending_air) > _PENDING_AIR_MAX:  # 송출 통보가 없는 실행(로컬)에서 쌓이지 않게
            self._pending_air.pop(next(iter(self._pending_air)))

        # [6] 패키징
        return SegmentSubmission(
            id=seg_id,
            station_id=self.station_id,
            audio_ref=to_audio_ref(self.audio_root, out_path),
            duration_ms=duration_ms,
            corner_type=job.corner_type,
            kind=SegmentKind.FILLER if job.step == "filler" else SegmentKind.SPEECH,
            priority=job.priority,
            reorderable=job.reorderable,
            request_ref=quote_ref,
            music_ref=job.material.track_ref,
        )

    async def _fallback(self, job: GenerationJob) -> SegmentSubmission | None:
        """고정 filler 대본으로 대체한다. 여기서의 TTS 실패는 스케줄러까지 올라간다.

        곡 소개였다면 곡(music_ref)은 그대로 싣는다 — 백엔드는 이 멘트 뒤에 곡을 튼다.
        """
        job.provider_failed = True
        filler = GenerationJob(job_id=job.job_id, corner_type="filler", step="filler",
                               material=Material(track=job.material.track),
                               priority=job.priority)
        now = time.time()
        self._log(job, "fallback", now, now, "filler")
        prompt = build_prompt("filler", filler.material, self.profile)
        script = await self._fallback_llm.generate(prompt)
        try:
            return await self._finish(filler, script, now)
        except _TtsFailed as exc:
            raise RuntimeError(f"filler 대체도 TTS에서 실패: {job.job_id}") from exc.__cause__

    async def _retry(self, job: GenerationJob, stage: str,
                     call: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(_ATTEMPTS):
            started = time.time()
            try:
                return await call()
            except Exception as exc:
                self._log(job, stage, started, time.time(), "error")
                if attempt == _ATTEMPTS - 1 or isinstance(exc, TtsQuotaError):  # 한도는 재시도해도 같다
                    raise
                logger.warning("RETRY %s %s %s: %r", stage, job.job_id, job.corner_type, exc)
        raise AssertionError("unreachable")

    def _with_context(self, prompt: Prompt) -> Prompt:
        if not self._recent:
            return prompt
        # user가 아니라 system에 붙인다 — user는 코너 지시와 소재만 담는다.
        # 직전 대본은 사연을 인용할 수 있으므로 데이터 블록으로 넣는다 — 지연된 프롬프트 인젝션 대비
        recent = "\n".join(f"- {text}" for text in self._recent)
        return Prompt(
            system=(f"{prompt.system}\n\n직전에 송출한 멘트다. 흐름은 이어가되 같은 인사·표현을 "
                    f"반복하지 않는다.\n{data_block(RECENT, recent)}"),
            user=prompt.user,
        )

    def _check_quote(self, job: GenerationJob, kind: str, chat: str, script: str, started: float) -> bool:
        """인용 검사 하나 — 판정과 함께 채팅·대본·일치 비율을 남긴다. 누락이 LLM이 건너뛴 것인지
        풀어 말한 것을 검사가 못 알아본 것인지는 대본을 읽어야 갈린다 (ENGINE_TUNING_LOG O-001)."""
        match = quote_match(chat, script)
        ok = quoted(chat, script) if kind == "chat" else match >= QUOTE_MATCH  # 낭독은 풀어 말하면 안 된다
        self.telemetry.log_quote(
            job_id=job.job_id, corner_type=job.corner_type, step=job.step,
            request_ref=job.material.quotes[0].request_id if job.material.quotes else job.material.feature_ref,
            kind=kind, result="ok" if ok else "missing", match=match, chat=chat, script=script, at=started)
        self._log(job, "quote", started, time.time(), "ok" if ok else "missing")
        return ok

    def _log(self, job: GenerationJob, stage: str, started: float, finished: float,
             result: str) -> None:
        request_ref = (job.material.quotes[0].request_id if job.material.quotes
                       else job.material.feature_ref)
        self.telemetry.log_stage(
            job_id=job.job_id, corner_type=job.corner_type, request_ref=request_ref,
            stage=stage, started_at=started, finished_at=finished, result=result,
        )


class _TtsFailed(Exception):
    """TTS가 재시도까지 실패 — 원인은 __cause__."""
