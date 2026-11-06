// 테스트 방송 정보 — 어드민이 만든 방이라 소유자 조회로 설정(코너·페르소나)과 시작 시각을 안다
import { createStationClient } from '@/routes/station-create/client'
import { getAccessToken } from '@/shared/authToken'

const stationClient = createStationClient(import.meta.env.VITE_API_BASE_URL ?? '', getAccessToken)

export const TEST_STATION_KEY = 'test-station'

export function testStationQuery(stationId: string | undefined) {
  return {
    queryKey: [TEST_STATION_KEY, stationId],
    queryFn: () => stationClient.getStation(stationId!),
    enabled: Boolean(stationId),
    // 다시 시작하면 같은 station_id의 새 방송이라 캐시를 쓰지 않고 새로 받는다
    refetchOnMount: 'always' as const,
    staleTime: Infinity,
    retry: false,
  }
}
