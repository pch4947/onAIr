"""생성 파이프라인 — 프롬프트 → LLM(L1) → 인용 검사 → L2 → TTS → 패키징 (설계 문서 4.4).

모든 단계 경계에서 타임스탬프를 기록한다. 안전 검사는 전부 제출 이전에 끝난다.
실패 처리(MVP 규칙, 설계 문서 4.4): LLM/TTS 호출은 1회 재시도하고, 재실패하면
더미 LLM의 고정 filler 대본으로 대체한다 — 방송은 제공자 장애로 멈추지 않는다.
"""
from __future__ import annotations

import itertools
import logging
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
from .tts import TtsClient

_ATTEMPTS = 2  # 최초 1회 + 재시도 1회
# 인용 검사 — 채팅 글자 bigram 중 이만큼이 대본에 있으면 인용한 것으로 본다 [예시].
# LLM은 "컵라면이요"를 "컵라면이라고"처럼 어미를 바꿔 인용하므로 문자열 그대로는 비교하지 않는다
QUOTE_MATCH = 0.5


def quoted(chat: str, script: str) -> bool:
    """대본에 채팅이 인용됐는가 (CORNER_DESIGN 5.1절 — 넣었으면 반드시 인용한다, 빠졌으면 되돌린다)."""
    def bigrams(text: str) -> set[str]:
        chars = [c for c in text if c.isalnum()]
        return {a + b for a, b in itertools.pairwise(chars)}

    want = bigrams(chat)
    if not want:  # 'ㅋㅋ'처럼 글자가 없는 채팅 — 비교할 것이 없으니 인용된 것으로 본다
        return True
    return len(want & bigrams(script)) / len(want) >= QUOTE_MATCH

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
        self._log(job, "llm", t0, t1, "ok")
        if script.text.strip() == REJECT:
            self._log(job, "l1", t1, t1, "reject")
            return None

        # [3] 인용 검사 — 빠진 입력은 request_ref를 비워 스케줄러가 되돌리게 한다
        quote_ref = None
        if job.material.quotes:
            first = job.material.quotes[0]
            ok = quoted(first.body, script.text)
            self._log(job, "quote", t1, time.time(), "ok" if ok else "missing")
            quote_ref = first.request_id if ok else None

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
                if attempt == _ATTEMPTS - 1:
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

    def _log(self, job: GenerationJob, stage: str, started: float, finished: float,
             result: str) -> None:
        request_ref = job.material.quotes[0].request_id if job.material.quotes else None
        self.telemetry.log_stage(
            job_id=job.job_id, corner_type=job.corner_type, request_ref=request_ref,
            stage=stage, started_at=started, finished_at=finished, result=result,
        )


class _TtsFailed(Exception):
    """TTS가 재시도까지 실패 — 원인은 __cause__."""
