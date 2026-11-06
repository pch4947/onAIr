import { useNavigate } from 'react-router-dom'
import { clearAccessToken } from '@/shared/authToken'

export function isUnauthorizedError(error: unknown): boolean {
  return error instanceof Error && error.message === 'UNAUTHORIZED'
}

export function LoginExpiredNotice({ className }: { className?: string }) {
  const navigate = useNavigate()
  return (
    <p role="alert" className={`flex items-center gap-1.5 text-danger ${className ?? ''}`}>
      로그인이 만료됐어요.
      <button
        type="button"
        onClick={() => { clearAccessToken(); navigate('/login') }}
        className="font-semibold underline"
      >
        다시 로그인
      </button>
    </p>
  )
}
