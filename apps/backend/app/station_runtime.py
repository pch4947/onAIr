"""방송(채널)마다 하나씩 두는 저장소·작업자 묶음 (docs/BACKEND_MULTI_STATION.md 4절).

채널은 설정(ONAIR_STATION_IDS)으로 정한다. 방송 ID가 없는 기존 경로는 첫 채널(기본 방송)을 쓴다.
"""
from dataclasses import replace

from fastapi import HTTPException, Request


class StationRuntime:
    """방송 하나의 객체들. Redis·엔진 HTTP 클라이언트는 앱 공용을 빌려 쓴다.

    첫 노래 준비, 종료 정리, 엔진 수신기는 이 객체를 상태 묶음으로 받는다.
    """

    def __init__(self, shared, settings):
        self._shared = shared
        self.settings = settings
        self.station_id = settings.station_id
        # redis_lifespan이 Redis 연결과 함께 채운다
        self.stations = self.requests = self.presence = None
        self.replay_sessions = self.replay_bookmarks = None
        self.music = self.story_feed = None
        self.engine_consumer = self.hls = self.lifecycle = None

    @property
    def redis(self):
        return self._shared.redis

    @property
    def engine_http(self):
        return self._shared.engine_http

    @property
    def engine_status(self):
        return getattr(self._shared, 'engine_status', None)


def ensure_runtimes(state) -> dict[str, StationRuntime]:
    if getattr(state, 'runtimes', None) is None:
        settings = state.settings
        ids = getattr(settings, 'channel_ids', None) or (settings.station_id,)
        # 채널마다 방송 ID만 바꾼 설정 사본. Redis 키·HLS·음원 디렉터리가 방송 ID로 나뉜다
        state.runtimes = {sid: StationRuntime(state, settings if sid == settings.station_id
                                              else replace(settings, station_id=sid)) for sid in ids}
        state.default_station_id = ids[0]
    return state.runtimes


def default_runtime(app) -> StationRuntime:
    return ensure_runtimes(app.state)[app.state.default_station_id]


def station_runtime(request: Request, station_id: str) -> StationRuntime:
    station = ensure_runtimes(request.app.state).get(station_id)
    if station is None:
        raise HTTPException(404, 'Station not found')
    return station
