// 06_Auth_로그인

import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { AuthBrandPanel } from '@/routes/auth/AuthBrandPanel'
import { SOCIAL_PROVIDERS } from '@/routes/auth/mock'
import { login } from '@/shared/api'
import { setAccessToken } from '@/shared/authToken'

const SOCIAL_BUTTON_CLASS: Record<string, string> = {
  naver: 'bg-[#03c75a] text-white',
  kakao: 'bg-[#fee500] text-[#1a1a1f]',
  google: 'border border-border bg-white text-text',
}

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder'

export function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const signupEmail = (location.state as { signupEmail?: string } | null)?.signupEmail
  const [email, setEmail] = useState(signupEmail ?? '')
  const [password, setPassword] = useState('')
  const [keepSignedIn, setKeepSignedIn] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setSubmitError('')
    setIsSubmitting(true)
    try {
      const { accessToken } = await login(email.trim(), password)
      setAccessToken(accessToken)
      navigate('/main')
    } catch (error) {
      setSubmitError(
        error instanceof Error && error.message === 'INVALID_CREDENTIALS'
          ? '이메일 또는 비밀번호가 올바르지 않습니다'
          : '로그인에 실패했어요, 잠시 후 다시 시도해주세요',
      )
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen">
      <AuthBrandPanel />

      <main className="flex flex-1 items-center justify-center bg-surface p-8">
        <form onSubmit={handleSubmit} className="flex w-full max-w-[360px] flex-col gap-5">
          <h2 className="text-[28px] font-bold text-text">로그인</h2>

          {signupEmail && (
            <p className="rounded-md bg-[#ebf2ff] px-3.5 py-2.5 text-xs font-medium text-primary">
              회원가입이 완료되었어요. 로그인해주세요.
            </p>
          )}

          <label className="flex flex-col gap-1.5">
            <span className={fieldLabelClass}>이메일</span>
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="you@example.com"
              className={textInputClass}
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className={fieldLabelClass}>비밀번호</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="비밀번호를 입력하세요"
              className={textInputClass}
            />
          </label>

          <div className="flex items-center justify-between text-[13px]">
            <label className="flex items-center gap-2 text-text-muted">
              <input
                type="checkbox"
                checked={keepSignedIn}
                onChange={(event) => setKeepSignedIn(event.target.checked)}
              />
              로그인 상태 유지
            </label>
            <button type="button" className="font-medium text-primary">
              비밀번호를 잊으셨나요?
            </button>
          </div>

          {submitError && <p className="text-xs text-danger">{submitError}</p>}

          <button
            type="submit"
            disabled={isSubmitting}
            className="rounded-md bg-primary py-3 text-sm font-semibold text-white disabled:opacity-60"
          >
            {isSubmitting ? '로그인 중...' : '로그인'}
          </button>

          <div className="flex items-center gap-3 text-xs text-text-muted">
            <span className="h-px flex-1 bg-border" />
            또는
            <span className="h-px flex-1 bg-border" />
          </div>

          <div className="flex flex-col gap-2.5">
            {SOCIAL_PROVIDERS.map((provider) => (
              <button
                key={provider.id}
                type="button"
                className={`rounded-md py-3 text-sm font-semibold ${SOCIAL_BUTTON_CLASS[provider.id]}`}
              >
                {provider.label}
              </button>
            ))}
          </div>

          <p className="text-center text-[13px] text-text-muted">
            계정이 없으신가요?{' '}
            <button
              type="button"
              onClick={() => navigate('/signup')}
              className="font-semibold text-primary"
            >
              회원가입
            </button>
          </p>
        </form>
      </main>
    </div>
  )
}
