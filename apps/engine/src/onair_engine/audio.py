"""오디오 파일 관리 — 원자적 쓰기, TTS 결과 캐시, 길이 실측.

백엔드는 메시지를 받은 즉시 audio_ref 파일을 읽을 수 있어야 한다. 그래서 파일은 항상
같은 디렉토리의 임시 이름(.으로 시작)으로 쓴 뒤 os.replace로 한 번에 드러낸다.
계약: docs/ENGINE_REDIS_CONTRACT.md 5장.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import shutil
import uuid
import wave
from pathlib import Path

from .pipeline.normalize import normalize_for_tts
from .pipeline.safety import split_sentences
from .pipeline.tts import TtsClient, measure_duration_ms

logger = logging.getLogger(__name__)

# 나눠 합성한 조각 사이에 두는 틈 [임시값] — 조각의 끝과 처음이 맞붙어 한 낱말처럼 들리지 않게 한다
JOIN_GAP_MS = 150
# 조각 하나의 길이는 나누는 기준의 이만큼을 넘지 않게 잡는다 — 기준 150자면 조각은 130자쯤까지
_PART_RATIO = 0.87


def temp_path_for(path: Path) -> Path:
    # 확장자를 유지해야 mutagen이 임시 파일에서도 포맷을 판별한다
    return path.with_name(f".{path.stem}.{uuid.uuid4().hex[:8]}.tmp{path.suffix}")


class CachedTts:
    """TtsClient를 감싸 원자적 쓰기 + 텍스트 단위 캐시 + 길이 실측을 더한다.

    접착 문구·filler처럼 같은 문장이 반복되면 API를 다시 호출하지 않는다 (비용·지연 절감).
    캐시는 엔진 전용이며 백엔드와 공유하는 오디오 루트 밖에 둔다.
    """

    def __init__(self, inner: TtsClient, cache_dir: Path | None = None, pause_ms: int = 0,
                 split_chars: int = 0):
        """split_chars가 0보다 크면 그보다 긴 대본을 문장 경계에서 나눠 동시에 합성하고 한 파일로 붙인다 (#342).

        합성에 걸리는 시간은 대본 길이에 따라 늘어난다 — 나누면 가장 긴 조각의 시간으로 줄어든다. 세그먼트는
        그대로 하나라 편성은 바뀌지 않는다. WAV를 내는 TTS에서만 나눈다.
        """
        self._inner = inner
        self._split_chars = max(0, int(split_chars))
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self.file_ext = inner.file_ext
        # 멘트 끝에 붙이는 쉬는 시간 — 멘트가 숨 쉴 틈 없이 이어 붙지 않게 한다 (2026-11-15 실제 방송 피드백).
        # 길이가 다르면 다른 파일이다 — 캐시를 따로 둔다
        self._pause_ms = max(0, int(pause_ms))
        self.cache_namespace = (inner.cache_namespace + (f"+pause{self._pause_ms}" if self._pause_ms else "")
                                + (f"+split{self._split_chars}" if self._split_chars else ""))
        self.hits = 0
        self.misses = 0

    @property
    def blocked_until(self) -> float:
        return getattr(self._inner, "blocked_until", 0.0)

    async def synthesize(self, text: str, out_path: Path) -> int:
        text = normalize_for_tts(text)  # 캐시 키도 정규화된 텍스트 기준이다
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if self._cache_dir is None:
            return await self._render(text, out_path)

        cached = self.cache_path(text)
        duration_ms = measure_duration_ms(cached) if cached.exists() else None
        if duration_ms is None:  # 캐시 없음 또는 손상 — 새로 합성한다
            self.misses += 1
            cached.parent.mkdir(parents=True, exist_ok=True)
            duration_ms = await self._render(text, cached)
        else:
            self.hits += 1
        _publish(cached, out_path)
        return duration_ms

    def cache_path(self, text: str) -> Path:
        assert self._cache_dir is not None
        digest = hashlib.sha256(f"{self.cache_namespace}\n{text}".encode()).hexdigest()
        return self._cache_dir / self.cache_namespace / digest[:2] / f"{digest}{self.file_ext}"

    def parts(self, text: str) -> list[str]:
        """이 대본을 합성할 때의 조각들 — 나누지 않으면 하나. 문장은 쪼개지 않고 조각 길이를 고르게 잡는다."""
        text = normalize_for_tts(text)
        if not self._split_chars or self.file_ext != ".wav" or len(text) <= self._split_chars:
            return [text]
        return split_evenly(split_sentences(text), self._split_chars * _PART_RATIO)

    async def _render_parts(self, parts: list[str], dest: Path) -> int | None:
        """조각을 동시에 합성해 dest에 이어 쓴다. 이어 붙일 수 없으면(WAV가 아니다, 형식이 다르다) None.

        캐시가 있으면 조각도 캐시한다 — 한 조각이 실패해 다시 부를 때 나머지는 다시 합성하지 않는다.
        """
        files = [self._part_path(part, dest, n) for n, part in enumerate(parts)]
        try:
            # 한 조각이 실패해도 나머지는 끝까지 간다 — 캐시에 남아, 다시 부를 때 실패한 조각만 합성한다
            done = await asyncio.gather(*(self._render_part(part, path)
                                          for part, path in zip(parts, files, strict=True)),
                                        return_exceptions=True)
            for result in done:
                if isinstance(result, BaseException):
                    raise result
            if not join_wavs(files, dest, JOIN_GAP_MS):
                logger.warning("TTS_SPLIT_UNJOINABLE %d조각 — 통째로 합성한다", len(parts))
                return None
        finally:
            if self._cache_dir is None:
                for path in files:
                    path.unlink(missing_ok=True)
        return measure_duration_ms(dest) or 0

    def _part_path(self, part: str, dest: Path, n: int) -> Path:
        if self._cache_dir is None:
            return temp_path_for(dest.with_name(f"{dest.stem}.part{n}{dest.suffix}"))
        digest = hashlib.sha256(f"{self._inner.cache_namespace}\n{part}".encode()).hexdigest()
        return self._cache_dir / f"{self._inner.cache_namespace}+part" / digest[:2] / f"{digest}{self.file_ext}"

    async def _render_part(self, part: str, path: Path) -> None:
        if self._cache_dir is not None and path.exists() and measure_duration_ms(path):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = temp_path_for(path)
        try:
            await self._inner.synthesize(part, tmp)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    async def _render(self, text: str, dest: Path) -> int:
        tmp = temp_path_for(dest)
        try:
            parts = self.parts(text)
            estimate = await self._render_parts(parts, tmp) if len(parts) > 1 else None
            if estimate is None:
                estimate = await self._inner.synthesize(text, tmp)
            if self._pause_ms and append_silence(tmp, self._pause_ms):
                estimate += self._pause_ms
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return measure_duration_ms(dest) or estimate


def split_evenly(sentences: list[str], max_chars: float) -> list[str]:
    """문장들을 차례를 지켜 묶는다 — 가장 긴 묶음이 가장 짧아지게. 묶음 수는 전체 길이를 max_chars로 나눈 만큼."""
    if len(sentences) < 2:
        return [" ".join(sentences)] if sentences else []
    count = min(len(sentences), max(2, math.ceil(sum(map(len, sentences)) / max_chars)))
    # best[k][i]: 앞의 문장 i개를 k묶음으로 나눌 때 가장 긴 묶음의 길이
    sizes = [0]
    for sentence in sentences:
        sizes.append(sizes[-1] + len(sentence))
    best = [[math.inf] * (len(sentences) + 1) for _ in range(count + 1)]
    cut = [[0] * (len(sentences) + 1) for _ in range(count + 1)]
    best[0][0] = 0
    for k in range(1, count + 1):
        for i in range(k, len(sentences) + 1):
            for j in range(k - 1, i):
                longest = max(best[k - 1][j], sizes[i] - sizes[j])
                if longest < best[k][i]:
                    best[k][i], cut[k][i] = longest, j
    groups, end = [], len(sentences)
    for k in range(count, 0, -1):
        start = cut[k][end]
        groups.append(" ".join(sentences[start:end]))
        end = start
    return groups[::-1]


def join_wavs(paths: list[Path], dest: Path, gap_ms: int = 0) -> bool:
    """WAV 여러 개를 차례로 이어 dest에 쓴다. WAV가 아니거나 형식이 서로 다르면 쓰지 않고 False."""
    frames, params = [], None
    try:
        for path in paths:
            with wave.open(str(path), "rb") as src:
                shape = (src.getnchannels(), src.getsampwidth(), src.getframerate(), src.getcomptype())
                if params is None:
                    params = src.getparams()
                elif shape != (params.nchannels, params.sampwidth, params.framerate, params.comptype):
                    return False
                frames.append(src.readframes(src.getnframes()))
    except (wave.Error, EOFError, OSError):
        return False
    if params is None:
        return False
    zero = b"\x80" if params.sampwidth == 1 else b"\x00" * params.sampwidth
    gap = zero * params.nchannels * (params.framerate * max(0, gap_ms) // 1000)
    with wave.open(str(dest), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(gap.join(frames))
    return True


def append_silence(path: Path, ms: int) -> bool:
    """WAV 파일 끝에 무음을 붙인다. WAV(PCM)가 아니면 그대로 두고 False."""
    try:
        with wave.open(str(path), "rb") as src:
            params = src.getparams()
            frames = src.readframes(src.getnframes())
    except (wave.Error, EOFError):
        return False
    zero = b"\x80" if params.sampwidth == 1 else b"\x00" * params.sampwidth  # 8비트 PCM의 무음은 0x80이다
    pad = zero * params.nchannels * (params.framerate * ms // 1000)
    tmp = temp_path_for(path)
    try:
        with wave.open(str(tmp), "wb") as dst:
            dst.setparams(params)
            dst.writeframes(frames + pad)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def _publish(src: Path, dest: Path) -> None:
    """캐시 파일을 dest로 드러낸다. 하드링크로 복사 비용을 없애고, 안 되면 복사한다.

    하드링크는 캐시와 내용을 공유하므로 dest 파일을 제자리 수정하면 안 된다 (계약상 읽기 전용).
    """
    tmp = temp_path_for(dest)
    try:
        try:
            os.link(src, tmp)
        except OSError:  # 다른 볼륨이거나 링크를 지원하지 않는 파일시스템
            shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
