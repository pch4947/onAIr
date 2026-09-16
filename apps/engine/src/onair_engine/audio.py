"""오디오 파일 관리 — 원자적 쓰기, TTS 결과 캐시, 길이 실측.

백엔드는 메시지를 받은 즉시 audio_ref 파일을 읽을 수 있어야 한다. 그래서 파일은 항상
같은 디렉토리의 임시 이름(.으로 시작)으로 쓴 뒤 os.replace로 한 번에 드러낸다.
계약: docs/ENGINE_REDIS_CONTRACT.md 5장.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from .pipeline.tts import TtsClient, measure_duration_ms


def temp_path_for(path: Path) -> Path:
    # 확장자를 유지해야 mutagen이 임시 파일에서도 포맷을 판별한다
    return path.with_name(f".{path.stem}.{uuid.uuid4().hex[:8]}.tmp{path.suffix}")


class CachedTts:
    """TtsClient를 감싸 원자적 쓰기 + 텍스트 단위 캐시 + 길이 실측을 더한다.

    ack·filler처럼 같은 문장이 반복되면 API를 다시 호출하지 않는다 (비용·지연 절감).
    캐시는 엔진 전용이며 백엔드와 공유하는 오디오 루트 밖에 둔다.
    """

    def __init__(self, inner: TtsClient, cache_dir: Path | None = None):
        self._inner = inner
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self.file_ext = inner.file_ext
        self.cache_namespace = inner.cache_namespace
        self.hits = 0
        self.misses = 0

    async def synthesize(self, text: str, out_path: Path) -> int:
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

    async def _render(self, text: str, dest: Path) -> int:
        tmp = temp_path_for(dest)
        try:
            estimate = await self._inner.synthesize(text, tmp)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return measure_duration_ms(dest) or estimate


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
