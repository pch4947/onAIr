"""생성 파이프라인 — 프롬프트 → LLM(L1) → 인용 검사 → L2 → TTS → 패키징 (설계 문서 4.4).

모든 단계 경계에서 타임스탬프를 기록한다. 안전 검사는 전부 제출 이전에 끝난다.
실패 처리(MVP 규칙, 설계 문서 4.4): LLM/TTS 호출은 1회 재시도하고, 재실패하면
더미 LLM의 고정 filler 대본으로 대체한다 — 방송은 제공자 장애로 멈추지 않는다.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from ..corners.steps import build_prompt, split_block
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
from .llm import REJECT, DummyLlmClient, LlmClient, LlmError
from .safety import SafetyChecker, Subjective, split_sentences
from .tts import TtsClient, TtsQuotaError

_ATTEMPTS = 2  # 최초 1회 + 재시도 1회
# 앞 멘트의 대본이 나올 때까지 기다리는 상한 — 넘으면 앞 멘트를 모르는 채로 쓴다. 앞 호출이 느릴 때
# 뒤 멘트까지 늦어져 무음이 되는 것보다 낫다 (LLM 호출은 보통 1.5초, 드물게 20초)
_CONTEXT_WAIT_SEC = 3.0
BLOCK_MAX_TOKENS = 1500  # 덩어리는 멘트 서너 개 분량이다. 추론 모델은 생각하는 데도 출력 한도를 쓴다
_FAILED = object()  # _write: LLM이 재시도까지 실패했다
# 뉴스 내용을 전하는 단계 — 주관 표현 규칙은 여기에만 건다. 뉴스 형식의 오프닝·엔딩 인사에는 걸지 않는다
_NEWS_STEP = "news_"
_PENDING_AIR_MAX = 256
# 인용 검사 — 채팅 글자 bigram 중 이만큼이 대본에 있으면 인용한 것으로 본다 [예시].
# LLM은 "컵라면이요"를 "컵라면이라고"처럼 어미를 바꿔 인용하므로 문자열 그대로는 비교하지 않는다
QUOTE_MATCH = 0.5
# 풀어서 한 인용 — 청취자가 한 말이라고 밝혔으면("~냐고 물어보신 분이 있네요") 일치 비율이 이만큼만 돼도
# 인용으로 본다. 2026-11-12 평가(채팅 162건)에서 누락 58건 중 43건이 이런 대본이었고, 그 일치 비율은
# 0.25~0.46에 몰려 있었다. 0.2 아래는 채팅의 낱말이 거의 남지 않은 대본이다 (ENGINE_TUNING_LOG T-006) [실험 T-006]
PARAPHRASE_MATCH = 0.2
# 청취자가 한 말이라고 밝히는 표현. "~하신 분이 있네요", "~라는 분도 계시네요", "~냐고 물어보신",
# 그리고 청취자를 바로 부르는 "~라고 하신 분, …" (인용 조사 뒤에 '분'이 온다)
_ATTRIBUTION = re.compile(
    r"(분|사람)(들)?(이|도|께서)?\s?(있|계시|계신|많)|물어보|물어본|물으신|물으셨"
    r"|(다고|라고|냐고|자고|다는|라는|냐는)[^.?!,]{0,12}(분|사람)")
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
        # 대본은 한 번에 하나씩 쓴다 — 뒤 멘트가 앞 멘트의 대본을 보고 쓰게 한다. 합성은 그대로 동시에 돈다.
        # 예전에는 합성이 끝나야 맥락에 들어가서, 동시에 만든 멘트의 절반이 앞 멘트를 모르고 썼다 (#262)
        self._script_lock = asyncio.Lock()
        self._persona_no = 0  # 페르소나를 바꾼 횟수 — 바꾸기 전에 쓰기 시작한 대본을 맥락에 넣지 않는다

    def switch_persona(self, profile: StationProfile, tts: TtsClient) -> None:
        """이 뒤로 쓰는 대본과 합성이 새 페르소나의 것이다 (#261).

        직전 멘트 맥락은 비운다 — 이전 DJ가 한 말이라 "방금 한 말에 이어서"가 맞지 않고, 말투가 옮아간다.
        """
        self.profile = profile
        self.tts = tts
        self._recent.clear()
        self._persona_no += 1

    def take_aired(self, segment_id: str) -> str | None:
        """PUBLISHED를 받은 세그먼트의 대본. 재전달이나 모르는 세그먼트면 None."""
        return self._pending_air.pop(segment_id, None)

    @property
    def tts_blocked_until(self) -> float:
        """TTS 한도 초과로 호출하지 않는 시각까지. 그동안 생성해도 합성할 수 없다."""
        return getattr(self.tts, "blocked_until", 0.0)

    async def run(self, job: GenerationJob) -> SegmentSubmission | None:
        """성공 시 제출 페이로드, REJECT 시 None.

        input_refs는 이 멘트가 반영한 청취자 입력이다 — 넣은 입력이 대본에 실제로 들어갔을 때만 싣는다.
        빠진 입력은 스케줄러가 되돌린다. request_ref는 그 첫 건이다(계약 v1). LLM/TTS가 재시도까지 실패하면 filler 대체
        세그먼트를 돌려준다(곡 소개였다면 music_ref는 유지한다 — 곡 재생이 멘트 실패에 막히지 않게).
        """
        if job.block is not None and job.part > 0:
            return await self._run_part(job)
        try:
            written = await self._write_in_turn(job)
        finally:
            if job.block is not None:  # 어떻게 끝났든 나머지 문단 작업이 기다리지 않게 한다
                job.block.fail()
        if written is _FAILED:
            return await self._fallback(job)
        if written is None:
            return None
        script, refs = written
        try:
            return await self._voice(job, script, time.time(), refs)
        except _TtsFailed:
            return await self._fallback(job)

    async def _write_in_turn(self, job: GenerationJob):
        """앞 멘트의 대본이 나온 뒤에 쓴다. 앞 호출이 오래 걸리면 기다리지 않고 쓴다."""
        try:
            await asyncio.wait_for(self._script_lock.acquire(), _CONTEXT_WAIT_SEC)
        except TimeoutError:
            return await self._write(job)
        try:
            return await self._write(job)
        finally:
            self._script_lock.release()

    async def _write(self, job: GenerationJob):
        """대본을 쓰고 검사해 맥락에 넣는다. (대본, 반영한 입력들) · None(거부) · _FAILED(LLM 장애)."""
        # [1] 프롬프트 구성 (L1 지시 내장 + 방송 맥락)
        t0 = time.time()
        persona_no = self._persona_no
        prompt = self._with_context(
            build_prompt(job.step, job.material, self.profile, plain=job.plain))

        # [2] LLM 호출
        try:
            if job.block is not None:
                texts = await self._retry(job, "llm", lambda: self._write_block(prompt, job.block.parts))
                script = Script(text=texts[0])
            else:
                script = await self._retry(job, "llm", lambda: self.llm.generate(prompt))
                texts = [script.text]
        except Exception:
            logger.exception("LLM_FAILED %s %s", job.job_id, job.corner_type)
            return _FAILED
        t1 = time.time()
        job.script_chars = len(script.text)
        self._log(job, "llm", t0, t1, "ok")
        if script.text.strip() == REJECT:
            self._log(job, "l1", t1, t1, "reject")
            return None

        # 뉴스 대본에 DJ의 의견·평가·전망이 섞였으면 한 번 다시 쓰고, 그래도 남으면 그 문장을 뺀다 (CORNER_DESIGN 6.4절)
        if job.step.startswith(_NEWS_STEP):
            script = await self._objective(job, prompt, script)
            if script is None:
                return None
            texts = [script.text]
            job.script_chars = len(script.text)

        # [3] 인용 검사 — 입력마다 본다. 빠진 입력은 싣지 않아 스케줄러가 되돌리게 한다
        refs: list[str] = []
        feature = job.material.feature_ref
        # 낭독은 대본에 글이 들어갔는지 본다. 사연·신청곡은 DJ가 풀어 말하므로 비교하지 않는다
        if feature and (job.material.must_read is None
                        or self._check_quote(job, "read", job.material.must_read, script.text, t1, feature)):
            refs.append(feature)
        for quote in job.material.quotes:
            if self._check_quote(job, "chat", quote.body, script.text, t1, quote.request_id):
                refs.append(quote.request_id)

        # [4] L2 출력단 검사 — 덩어리는 문단마다 보고, 걸린 문단부터 뒤는 버린다
        clean = list(itertools.takewhile(lambda text: self.safety.check_l2(text) is None, texts))
        self._log(job, "l2", t1, time.time(), "ok" if clean else "reject")
        if not clean:
            return None
        # 합성을 기다리지 않고 맥락에 넣는다 — 바로 뒤 멘트가 이 대본을 보고 쓴다.
        # 쓰는 사이 페르소나가 바뀌었으면 이전 DJ의 대본이고 방송에도 나가지 않는다 — 넣지 않는다
        if persona_no == self._persona_no:
            self._recent.extend(clean)
        if job.block is not None:
            job.block.resolve(clean)
        return script, refs

    async def _objective(self, job: GenerationJob, prompt: Prompt, script: Script) -> Script | None:
        """뉴스 대본에서 주관 표현을 걷어낸다 — 깨끗하면 그대로, 걸리면 다시 쓴 것이나 걸린 문장을 뺀 것. 남는 게 없으면 None.

        프롬프트 지시만으로는 결정적으로 지킬 수 없다. 다시 쓰기는 한 번만 한다 — 뉴스 멘트는 방송을 채우는
        분량이라 오래 붙잡지 않는다. 설계의 "소재 문장 요약 템플릿" 대신 걸린 문장만 뺀다: DJ 말투가 유지되고,
        남은 문장은 이미 소재에 근거한 사실이다.
        """
        phrases = self.profile.signature_phrases
        found = self.safety.check_news(script.text, phrases)
        if not found:
            return script
        started = time.time()
        self._note_subjective(job, found)
        note = ("\n방금 쓴 대본에 DJ의 의견·평가·전망으로 읽히는 표현이 있었다: "
                + ", ".join(sorted({hit.match for hit in found}))
                + ". 이런 표현과 그 문장 없이, 소재에 있는 사실만으로 다시 쓴다.")
        try:
            again = await self.llm.generate(Prompt(system=prompt.system, user=prompt.user + note))
        except Exception:  # 다시 쓰지 못했다 — 처음 대본에서 걸린 문장을 뺀다
            logger.exception("SUBJECTIVE_REWRITE_FAILED %s %s", job.job_id, job.corner_type)
            again = None
        if again is not None and again.text.strip() and again.text.strip() != REJECT:
            still = self.safety.check_news(again.text, phrases)
            if not still:
                self._log(job, "subjective", started, time.time(), "rewritten")
                return again
            self._note_subjective(job, still)
            if len(still) <= len(found):
                script, found = again, still
        bad = {hit.sentence for hit in found}
        kept = [s for s in split_sentences(script.text) if s not in bad]
        self._log(job, "subjective", started, time.time(), "trimmed" if kept else "reject")
        return Script(text=" ".join(kept)) if kept else None

    def _note_subjective(self, job: GenerationJob, found: list[Subjective]) -> None:
        for hit in found:
            logger.info("SUBJECTIVE %s [%s] %s: %s", job.job_id, hit.rule, hit.match, hit.sentence)
        self.telemetry.log_subjective(job.job_id, job.corner_type, job.step, found)

    async def _write_block(self, prompt: Prompt, parts: int) -> list[str]:
        """이야기 덩어리를 한 번에 쓰고 문단으로 나눈다. 문단을 못 얻으면 LlmError — 다시 부른다."""
        raw = await self.llm.complete(prompt, max_tokens=BLOCK_MAX_TOKENS)
        texts = split_block(raw, parts)
        if not texts:
            raise LlmError("덩어리 대본에서 문단을 얻지 못했습니다")
        return texts

    async def _run_part(self, job: GenerationJob) -> SegmentSubmission | None:
        """덩어리의 둘째 문단부터 — 대본은 이미 쓰였고 검사도 끝났다. 합성만 한다."""
        texts = await job.block.wait()
        if texts is None or job.part >= len(texts):
            return None  # 덩어리를 쓰지 못했거나 문단이 계획보다 적게 나왔다
        script = Script(text=texts[job.part])
        job.script_chars = len(script.text)
        try:
            return await self._voice(job, script, time.time(), None)
        except _TtsFailed:
            return await self._fallback(job)

    async def _voice(self, job: GenerationJob, script: Script, started: float,
                     refs: list[str] | None = None) -> SegmentSubmission:
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
        self._log(job, "tts", started, time.time(), "ok")
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
            request_ref=refs[0] if refs else None,
            music_ref=job.material.track_ref,
            input_refs=list(refs or []),
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
        self._recent.append(script.text)
        try:
            return await self._voice(filler, script, now)
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
            system=(f"{prompt.system}\n\n직전에 한 멘트다. 아래로 갈수록 최근이고 마지막 줄이 방금 한 말이다. "
                    f"방금 한 말에 이어서 말하고, 같은 인사·표현·질문을 반복하지 않는다.\n"
                    f"{data_block(RECENT, recent)}"),
            user=prompt.user,
        )

    def _check_quote(self, job: GenerationJob, kind: str, chat: str, script: str, started: float,
                     request_ref: str) -> bool:
        """인용 검사 하나 — 판정과 함께 채팅·대본·일치 비율을 남긴다. 누락이 LLM이 건너뛴 것인지
        풀어 말한 것을 검사가 못 알아본 것인지는 대본을 읽어야 갈린다 (ENGINE_TUNING_LOG O-001)."""
        match = quote_match(chat, script)
        ok = quoted(chat, script) if kind == "chat" else match >= QUOTE_MATCH  # 낭독은 풀어 말하면 안 된다
        self.telemetry.log_quote(
            job_id=job.job_id, corner_type=job.corner_type, step=job.step,
            request_ref=request_ref,
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
