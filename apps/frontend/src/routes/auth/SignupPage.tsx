// 07_Auth_회원가입

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AuthBrandPanel } from '@/routes/auth/AuthBrandPanel'

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder'
const errorTextClass = 'block text-xs text-danger'

// 실제 서버 DB가 없어서 이름 중복은 이 목록으로만 시뮬레이션합니다.
const MOCK_TAKEN_NAMES = ['사용자', 'admin', 'onAIr', '관리자']

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const PASSWORD_PATTERN = /^(?=.*[A-Za-z])(?=.*\d).{8,}$/

interface SignupErrors {
  name?: string
  email?: string
  password?: string
  passwordConfirm?: string
}

function validateSignup(
  name: string,
  email: string,
  password: string,
  passwordConfirm: string,
): SignupErrors {
  const errors: SignupErrors = {}
  const trimmedName = name.trim()
  const trimmedEmail = email.trim()

  if (!trimmedName) {
    errors.name = '이름을 입력해주세요'
  } else if (trimmedName.length < 2) {
    errors.name = '이름은 2자 이상이어야 합니다'
  } else if (MOCK_TAKEN_NAMES.includes(trimmedName)) {
    errors.name = '이미 사용 중인 이름입니다'
  }

  if (!trimmedEmail) {
    errors.email = '이메일을 입력해주세요'
  } else if (!EMAIL_PATTERN.test(trimmedEmail)) {
    errors.email = '이메일 형식이 올바르지 않습니다'
  }

  if (!password) {
    errors.password = '비밀번호를 입력해주세요'
  } else if (!PASSWORD_PATTERN.test(password)) {
    errors.password = '영문, 숫자를 포함해 8자 이상 입력해주세요'
  }

  if (!passwordConfirm) {
    errors.passwordConfirm = '비밀번호 확인을 입력해주세요'
  } else if (passwordConfirm !== password) {
    errors.passwordConfirm = '비밀번호가 일치하지 않습니다'
  }

  return errors
}

export function SignupPage() {
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [passwordConfirm, setPasswordConfirm] = useState('')
  const [errors, setErrors] = useState<SignupErrors>({})

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    const nextErrors = validateSignup(name, email, password, passwordConfirm)
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length === 0) {
      navigate('/')
    }
  }

  return (
    <div className="flex min-h-screen">
      <AuthBrandPanel />

      <main className="flex flex-1 items-center justify-center bg-surface p-8">
        <form onSubmit={handleSubmit} className="flex w-full max-w-[360px] flex-col gap-3">
          <h2 className="text-[28px] font-bold text-text">회원가입</h2>

          <label className="flex flex-col gap-1.5">
            <span className={fieldLabelClass}>이름</span>
            <input
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="공백 제외 2글자 이상"
              className={textInputClass}
            />
            <span className={errorTextClass}>{errors.name || ' '}</span>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className={fieldLabelClass}>이메일</span>
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="you@example.com"
              className={textInputClass}
            />
            <span className={errorTextClass}>{errors.email || ' '}</span>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className={fieldLabelClass}>비밀번호</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="8자 이상 입력하세요"
              className={textInputClass}
            />
            <span className={errorTextClass}>{errors.password || ' '}</span>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className={fieldLabelClass}>비밀번호 확인</span>
            <input
              type="password"
              value={passwordConfirm}
              onChange={(event) => setPasswordConfirm(event.target.value)}
              placeholder="비밀번호를 다시 입력하세요"
              className={textInputClass}
            />
            <span className={errorTextClass}>{errors.passwordConfirm || ' '}</span>
          </label>

          <button
            type="submit"
            className="mt-3 rounded-md bg-primary py-3 text-sm font-semibold text-white"
          >
            회원가입
          </button>

          <p className="text-center text-[13px] text-text-muted">
            이미 계정이 있으신가요?{' '}
            <button
              type="button"
              onClick={() => navigate('/login')}
              className="font-semibold text-primary"
            >
              로그인
            </button>
          </p>
        </form>
      </main>
    </div>
  )
}
