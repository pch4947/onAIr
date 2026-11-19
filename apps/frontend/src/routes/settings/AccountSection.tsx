// 계정 설정 (#325) — 닉네임·비밀번호 변경, 기본 익명, 탈퇴. 일반 회원에게만 보인다.
// 화면 설정과 달리 서버에 저장되므로 항목마다 따로 바로 적용한다
import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Row, Section, Toggle } from '@/routes/settings/parts'
import { AccountError, changeName, changePassword, deleteAccount, updatePreferences } from '@/shared/account'
import { clearAccessToken } from '@/shared/authToken'
import type { AuthUser } from '@/shared/types'

const NAME_MAX = 50
const PASSWORD_MIN = 8
const inputClass =
  'h-9 w-full rounded-md border border-field-border bg-field-bg px-3 text-base text-text placeholder:text-field-placeholder md:text-[13px]'
const buttonClass = 'shrink-0 rounded-md bg-primary px-4 py-2 text-xs font-semibold text-white disabled:opacity-50'

const statusOf = (error: unknown) => (error instanceof AccountError ? error.status : 0)

function Message({ error, success }: { error?: string | null; success?: string | null }) {
  if (error) return <p role="alert" className="text-xs text-danger">{error}</p>
  if (success) return <p role="status" className="text-xs font-semibold text-primary">{success}</p>
  return null
}

function NameForm({ user, onSaved }: { user: AuthUser; onSaved: (user: AuthUser) => void }) {
  const [name, setName] = useState(user.name)
  const trimmed = name.trim()
  const mutation = useMutation({ mutationFn: changeName, onSuccess: onSaved })
  const error = mutation.isError
    ? statusOf(mutation.error) === 409 ? '이미 쓰는 닉네임이에요.' : '닉네임을 바꾸지 못했어요. 잠시 후 다시 시도해 주세요.'
    : null
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (trimmed && trimmed !== user.name) mutation.mutate(trimmed)
  }
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 px-4 py-3.5">
      <label htmlFor="account-name" className="text-sm font-medium text-text">닉네임</label>
      <p className="text-xs text-text-muted">바꾸면 내가 쓴 글·댓글과 보낸 채팅·사연의 이름도 바뀌어요. 방송에 이미 나간 내용은 그대로예요.</p>
      <div className="flex gap-2">
        <input
          id="account-name"
          value={name}
          maxLength={NAME_MAX}
          onChange={(event) => { setName(event.target.value); mutation.reset() }}
          className={inputClass}
        />
        <button type="submit" disabled={!trimmed || trimmed === user.name || mutation.isPending} className={buttonClass}>
          {mutation.isPending ? '바꾸는 중…' : '변경'}
        </button>
      </div>
      <Message error={error} success={mutation.isSuccess ? '닉네임을 바꿨어요.' : null} />
    </form>
  )
}

function PasswordForm() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const mutation = useMutation({
    mutationFn: () => changePassword(current, next),
    onSuccess: () => { setCurrent(''); setNext(''); setConfirm('') },
  })
  const tooShort = next.length > 0 && next.length < PASSWORD_MIN
  const mismatch = confirm.length > 0 && next !== confirm
  const error = tooShort ? `새 비밀번호는 ${PASSWORD_MIN}자 이상이어야 해요.`
    : mismatch ? '새 비밀번호가 서로 달라요.'
    : mutation.isError ? statusOf(mutation.error) === 403 ? '지금 비밀번호가 맞지 않아요.' : '비밀번호를 바꾸지 못했어요.'
    : null
  const touch = (set: (value: string) => void) => (value: string) => { set(value); mutation.reset() }
  return (
    <form
      onSubmit={(event) => { event.preventDefault(); if (current && next.length >= PASSWORD_MIN && next === confirm) mutation.mutate() }}
      className="flex flex-col gap-2 px-4 py-3.5"
    >
      <span className="text-sm font-medium text-text">비밀번호</span>
      <input type="password" autoComplete="current-password" placeholder="지금 비밀번호" aria-label="지금 비밀번호"
        value={current} onChange={(event) => touch(setCurrent)(event.target.value)} className={inputClass} />
      <input type="password" autoComplete="new-password" placeholder={`새 비밀번호 (${PASSWORD_MIN}자 이상)`} aria-label="새 비밀번호"
        value={next} onChange={(event) => touch(setNext)(event.target.value)} className={inputClass} />
      <input type="password" autoComplete="new-password" placeholder="새 비밀번호 확인" aria-label="새 비밀번호 확인"
        value={confirm} onChange={(event) => touch(setConfirm)(event.target.value)} className={inputClass} />
      <div className="flex items-center justify-between gap-2">
        <Message error={error} success={mutation.isSuccess ? '비밀번호를 바꿨어요.' : null} />
        <button
          type="submit"
          disabled={!current || next.length < PASSWORD_MIN || next !== confirm || mutation.isPending}
          className={`${buttonClass} ml-auto`}
        >
          {mutation.isPending ? '바꾸는 중…' : '변경'}
        </button>
      </div>
    </form>
  )
}

function DeleteAccount() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [password, setPassword] = useState('')
  const mutation = useMutation({
    mutationFn: () => deleteAccount(password),
    onSuccess: () => {
      clearAccessToken()
      queryClient.clear()
      navigate('/login', { replace: true })
    },
  })
  const status = statusOf(mutation.error)
  const error = !mutation.isError ? null
    : status === 409 ? '진행 중인 내 방송이 있어요. 방송을 먼저 종료한 뒤 탈퇴해 주세요.'
    : status === 403 ? '비밀번호가 맞지 않아요.'
    : '탈퇴하지 못했어요. 잠시 후 다시 시도해 주세요.'

  if (!open) {
    return (
      <Row label="계정 탈퇴" description="계정을 지워요. 되돌릴 수 없어요">
        <button type="button" onClick={() => setOpen(true)}
          className="rounded-md border border-danger px-3.5 py-2 text-xs font-semibold text-danger">
          탈퇴
        </button>
      </Row>
    )
  }
  return (
    <form
      onSubmit={(event) => { event.preventDefault(); if (password) mutation.mutate() }}
      className="flex flex-col gap-2 px-4 py-3.5"
    >
      <span className="text-sm font-medium text-danger">계정 탈퇴</span>
      <ul className="list-disc pl-4 text-xs text-text-muted">
        <li>계정과 알림, 내가 누른 좋아요가 지워져요.</li>
        <li>내가 쓴 글·댓글과 보낸 채팅·사연은 지워지지 않고 이름이 "탈퇴한 사용자"로 바뀌어요.</li>
        <li>진행 중인 내 방송이 있으면 탈퇴할 수 없어요.</li>
        <li>되돌릴 수 없어요.</li>
      </ul>
      <input type="password" autoComplete="current-password" placeholder="비밀번호를 입력해 확인해 주세요" aria-label="탈퇴 확인 비밀번호"
        value={password} onChange={(event) => { setPassword(event.target.value); mutation.reset() }} className={inputClass} />
      <Message error={error} />
      <div className="flex justify-end gap-2">
        <button type="button" onClick={() => { setOpen(false); setPassword(''); mutation.reset() }}
          className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted">
          취소
        </button>
        <button type="submit" disabled={!password || mutation.isPending}
          className="rounded-md bg-danger px-4 py-2 text-xs font-semibold text-white disabled:opacity-50">
          {mutation.isPending ? '탈퇴하는 중…' : '탈퇴하기'}
        </button>
      </div>
    </form>
  )
}

export function AccountSection({ user }: { user: AuthUser }) {
  const queryClient = useQueryClient()
  const saveUser = (next: AuthUser) => queryClient.setQueryData(['me'], next)
  const anonymousMutation = useMutation({ mutationFn: updatePreferences, onSuccess: saveUser })
  const defaultAnonymous = anonymousMutation.isPending
    ? Boolean(anonymousMutation.variables) : Boolean(user.defaultAnonymous)

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <Section title="계정">
        <NameForm user={user} onSaved={saveUser} />
        <PasswordForm />
        <Row label="기본으로 익명 보내기" description="사연과 신청곡을 보낼 때 '익명으로'를 미리 켜 둬요. 바꾸면 바로 저장돼요">
          <Toggle
            label="기본으로 익명 보내기"
            checked={defaultAnonymous}
            disabled={anonymousMutation.isPending}
            onChange={(next) => anonymousMutation.mutate(next)}
          />
        </Row>
        {anonymousMutation.isError && (
          <p role="alert" className="px-4 py-2 text-xs text-danger">저장하지 못했어요. 잠시 후 다시 시도해 주세요.</p>
        )}
        <DeleteAccount />
      </Section>
    </div>
  )
}
