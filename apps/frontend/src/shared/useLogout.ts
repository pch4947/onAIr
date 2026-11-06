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
    queryClient.removeQueries({ queryKey: ['me'] })
    navigate('/login')
  }
}
