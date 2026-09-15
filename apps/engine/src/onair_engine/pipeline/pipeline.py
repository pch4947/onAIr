"""생성 파이프라인 — 프롬프트 → LLM(L1) → L2 → TTS → 패키징 (설계 문서 4.4).

모든 단계 경계에서 타임스탬프를 기록한다. 안전 검사는 전부 제출 이전에 끝난다.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..domain import (
    GenerationJob,
    SegmentKind,
    SegmentSubmission,
    StationProfile,
    new_id,
    to_audio_ref,
)
from ..telemetry import Telemetry
from .llm import LlmClient
from .safety import SafetyChecker
from .tts import TtsClient

_KIND_BY_CORNER = {"filler": SegmentKind.FILLER}


class GenerationPipeline:
    def __init__(self, *, station_id: str, profile: StationProfile, corners: dict,
                 llm: LlmClient, tts: TtsClient, safety: SafetyChecker,
                 telemetry: Telemetry, audio_root: Path):
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

    async def run(self, job: GenerationJob) -> SegmentSubmission | None:
        """성공 시 제출 페이로드, REJECT 시 None.

        TODO(M2): 호출 실패·타임아웃 시 1회 재시도, 재실패 시 filler 대체.
        """
        corner = self.corners[job.corner_type]

        # [1] 프롬프트 구성 (L1 지시 내장)
        # TODO(M1): 최근 N개 세그먼트 요약(방송 맥락, 확인 10)을 프롬프트에 주입
        t0 = time.time()
        prompt = corner.build_prompt(job.material, self.profile)

        # [2] LLM 호출
        script = await self.llm.generate(prompt)
        t1 = time.time()
        self._log(job, "llm", t0, t1, "ok")
        if script.text.strip() == "REJECT":
            self._log(job, "l1", t1, t1, "reject")
            return None

        # [3] L2 출력단 검사
        violation = self.safety.check_l2(script.text)
        t2 = time.time()
        self._log(job, "l2", t1, t2, "reject" if violation else "ok")
        if violation:
            return None

        # [4] TTS
        seg_id = new_id("seg")
        out_path = self.audio_dir / f"{seg_id}.wav"
        duration_ms = await self.tts.synthesize(script.text, out_path)
        t3 = time.time()
        self._log(job, "tts", t2, t3, "ok")

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

    def _log(self, job: GenerationJob, stage: str, started: float, finished: float,
             result: str) -> None:
        request_ref = job.material.request.request_id if job.material.request else None
        self.telemetry.log_stage(
            job_id=job.job_id, corner_type=job.corner_type, request_ref=request_ref,
            stage=stage, started_at=started, finished_at=finished, result=result,
        )
