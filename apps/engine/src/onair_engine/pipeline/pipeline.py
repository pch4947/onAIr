"""생성 파이프라인 — 프롬프트 → LLM(L1) → L2 → TTS → 패키징 (설계 문서 4.4).

모든 단계 경계에서 타임스탬프를 기록한다. 안전 검사는 전부 제출 이전에 끝난다.
실패 처리(MVP 규칙, 설계 문서 4.4): LLM/TTS 호출은 1회 재시도하고, 재실패하면
더미 LLM의 고정 filler 대본으로 대체한다 — 방송은 제공자 장애로 멈추지 않는다.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

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
from ..telemetry import Telemetry
from .llm import REJECT, DummyLlmClient, LlmClient
from .safety import SafetyChecker
from .tts import TtsClient

_KIND_BY_CORNER = {"filler": SegmentKind.FILLER}
_ATTEMPTS = 2  # 최초 1회 + 재시도 1회

T = TypeVar("T")

logger = logging.getLogger(__name__)


class GenerationPipeline:
    def __init__(self, *, station_id: str, profile: StationProfile, corners: dict,
                 llm: LlmClient, tts: TtsClient, safety: SafetyChecker,
                 telemetry: Telemetry, audio_root: Path, recent_segments: int = 8):
        self.station_id = station_id
        self.profile = profile
        self.corners = corners
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

    async def run(self, job: GenerationJob) -> SegmentSubmission | None:
        """성공 시 제출 페이로드, REJECT 시 None.

        LLM/TTS가 재시도까지 실패하면 filler 대체 세그먼트를 돌려준다. 이때 corner_type은
        "filler", request_ref는 None이다 — 요청 답변이 아니므로 스케줄러가 요청을 되돌린다.
        """
        corner = self.corners[job.corner_type]

        # [1] 프롬프트 구성 (L1 지시 내장 + 방송 맥락)
        t0 = time.time()
        prompt = self._with_context(corner.build_prompt(job.material, self.profile))

        # [2] LLM 호출
        try:
            script = await self._retry(job, "llm", lambda: self.llm.generate(prompt))
        except Exception:
            logger.exception("LLM_FAILED %s %s", job.job_id, job.corner_type)
            return await self._fallback(job)
        t1 = time.time()
        self._log(job, "llm", t0, t1, "ok")
        if script.text.strip() == REJECT:
            self._log(job, "l1", t1, t1, "reject")
            return None

        try:
            return await self._finish(job, script, t1)
        except _TtsFailed:
            return await self._fallback(job)

    async def _finish(self, job: GenerationJob, script: Script,
                      started: float) -> SegmentSubmission | None:
        # [3] L2 출력단 검사
        violation = self.safety.check_l2(script.text)
        t2 = time.time()
        self._log(job, "l2", started, t2, "reject" if violation else "ok")
        if violation:
            return None

        # [4] TTS
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

        # [5] 패키징
        return SegmentSubmission(
            id=seg_id,
            station_id=self.station_id,
            audio_ref=to_audio_ref(self.audio_root, out_path),
            duration_ms=duration_ms,
            corner_type=job.corner_type,
            kind=_KIND_BY_CORNER.get(job.corner_type, SegmentKind.SPEECH),
            priority=job.priority,
            reorderable=job.reorderable,
            request_ref=job.material.request.request_id if job.material.request else None,
            music_ref=job.material.track_id,
        )

    async def _fallback(self, job: GenerationJob) -> SegmentSubmission | None:
        """고정 filler 대본으로 대체한다. 여기서의 TTS 실패는 스케줄러까지 올라간다."""
        filler = GenerationJob(job_id=job.job_id, corner_type="filler", material=Material(),
                               priority=job.priority)
        now = time.time()
        self._log(job, "fallback", now, now, "filler")
        prompt = self.corners["filler"].build_prompt(filler.material, self.profile)
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
                if attempt == _ATTEMPTS - 1:
                    raise
                logger.warning("RETRY %s %s %s: %r", stage, job.job_id, job.corner_type, exc)
        raise AssertionError("unreachable")

    def _with_context(self, prompt: Prompt) -> Prompt:
        if not self._recent:
            return prompt
        # user가 아니라 system에 붙인다 — user는 코너 지시와 소재만 담는다
        recent = "\n".join(f"- {text}" for text in self._recent)
        return Prompt(
            system=(f"{prompt.system}\n\n직전에 송출한 멘트다. 흐름은 이어가되 같은 인사·표현을 "
                    f"반복하지 않는다.\n{recent}"),
            user=prompt.user,
        )

    def _log(self, job: GenerationJob, stage: str, started: float, finished: float,
             result: str) -> None:
        request_ref = job.material.request.request_id if job.material.request else None
        self.telemetry.log_stage(
            job_id=job.job_id, corner_type=job.corner_type, request_ref=request_ref,
            stage=stage, started_at=started, finished_at=finished, result=result,
        )


class _TtsFailed(Exception):
    """TTS가 재시도까지 실패 — 원인은 __cause__."""
