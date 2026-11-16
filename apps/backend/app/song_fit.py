"""신청곡 판정 — 청취자가 신청한 곡이 방의 장르·분위기에 어울리는지 엔진에 묻는다 (계약 6.4절, #301).

통과가 기본이다. 엔진이 확실히 어울리지 않는다고 답한 곡만 거절한다 — 곡 정보를 못 얻었거나 엔진이
답하지 않으면(시간 초과, 연결 실패, 404, 5xx) 통과시킨다. 엔진 문제로 신청이 막히면 안 된다.
"""
import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

# 엔진은 4초 안에 판정하지 못하면 통과로 답한다. 그보다 조금 길게 기다린다
ENGINE_TIMEOUT_SEC = 5.0
# 곡 정보(oEmbed) 조회까지 합친 상한 — 청취자가 신청 화면에서 기다리는 시간이다
TOTAL_TIMEOUT_SEC = 9.0


async def song_fits(engine_http, station_id: str, video_id: str, lookup) -> bool:
    """어울리지 않는다고 판정된 곡만 False. lookup(video_id)은 (제목, 채널 이름) 또는 None을 준다."""
    if engine_http is None:
        return True
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SEC):
            found = await lookup(video_id)
            if not found:
                return True
            title, artist = found
            response = await engine_http.post(f'/v1/stations/{station_id}/song-requests/check',
                                              json={'title': title[:200], 'artist': (artist or '')[:200]},
                                              timeout=ENGINE_TIMEOUT_SEC)
        if response.status_code != 200:
            return True
        verdict = response.json()
    except (httpx.HTTPError, TimeoutError, ValueError):
        log.warning('Song fit check skipped for %s on %s', video_id, station_id, exc_info=True)
        return True
    return not (isinstance(verdict, dict) and verdict.get('judged') is True and verdict.get('fit') is False)
