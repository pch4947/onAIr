import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { logoutSession } from '@/shared/api'
import { clearAccessToken } from '@/shared/authToken'

export function useLogout() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  return async () => {
    try {
      await logoutSession()
    } catch {
      // 서버에 알리지 못해도 이 기기에서는 로그아웃한다 (게스트 계정은 남을 수 있음)
    }
    clearAccessToken()
    // 내 정보만이 아니라 알림·내 방송·좋아요처럼 계정마다 다른 캐시를 모두 버린다 —
    // 다음에 로그인한 사람에게 앞 사람의 알림 수나 "내 방송으로 가기"가 잠깐 보인다 (#366)
    queryClient.clear()
    navigate('/login')
  }
}
