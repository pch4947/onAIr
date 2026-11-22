import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getMe } from '@/shared/api'
import { clearAccessToken, getAccessToken } from '@/shared/authToken'

// 토큰이 거절됐을 때(401)만 로그아웃한다. 503·네트워크 오류처럼 서버가 잠깐 못 답한 것까지 로그아웃하면
// 배포 중에 탭으로 돌아온 사용자가 로그인 화면으로 쫓겨난다 (#366)
const isRejected = (error: Error | null) => error?.message === 'UNAUTHORIZED'

export function useCurrentUser() {
  const navigate = useNavigate()
  const hasToken = Boolean(getAccessToken())
  const query = useQuery({
    queryKey: ['me'],
    queryFn: getMe,
    enabled: hasToken,
    retry: (count, error) => !isRejected(error) && count < 2,
  })
  const rejected = isRejected(query.error)

  useEffect(() => {
    if (!hasToken || rejected) {
      clearAccessToken()
      navigate('/login', { replace: true })
    }
  }, [hasToken, rejected, navigate])

  return query
}
