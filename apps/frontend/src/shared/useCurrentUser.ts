import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getMe } from '@/shared/api'
import { clearAccessToken, getAccessToken } from '@/shared/authToken'

export function useCurrentUser() {
  const navigate = useNavigate()
  const hasToken = Boolean(getAccessToken())
  const query = useQuery({
    queryKey: ['me'],
    queryFn: getMe,
    enabled: hasToken,
    retry: false,
  })

  useEffect(() => {
    if (!hasToken || query.isError) {
      clearAccessToken()
      navigate('/login')
    }
  }, [hasToken, query.isError, navigate])

  return query
}
