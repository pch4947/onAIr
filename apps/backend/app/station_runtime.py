"""방송(채널)마다 하나씩 두는 저장소·작업자 묶음 (docs/BACKEND_MULTI_STATION.md 4절).

지금은 설정한 방송 하나만 만든다. 방송 ID가 없는 기존 경로는 기본 방송을 쓴다.
"""
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


def ensure_runtimes(state) -> dict[str, StationRuntime]:
    if getattr(state, 'runtimes', None) is None:
        station = StationRuntime(state, state.settings)
        state.runtimes = {station.station_id: station}
        state.default_station_id = station.station_id
    return state.runtimes


def default_runtime(app) -> StationRuntime:
    return ensure_runtimes(app.state)[app.state.default_station_id]


def station_runtime(request: Request, station_id: str) -> StationRuntime:
    station = ensure_runtimes(request.app.state).get(station_id)
    if station is None:
        raise HTTPException(404, 'Station not found')
    return station
