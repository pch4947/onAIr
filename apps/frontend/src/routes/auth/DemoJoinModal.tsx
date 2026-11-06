// 데모용 참가 — 닉네임만으로 게스트 참가, 핀으로 어드민 참가

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { joinAsAdmin, joinAsGuest } from '@/shared/api'
import { setAccessToken } from '@/shared/authToken'

export type DemoJoinMode = 'guest' | 'admin'

const NICKNAME_MAX_LENGTH = 20
const PIN_MAX_LENGTH = 8

const ERROR_TEXT: Record<string, string> = {
  NAME_TAKEN: '이미 사용 중인 닉네임이에요. 다른 닉네임을 입력해주세요.',
  INVALID_PIN: '핀 번호가 맞지 않아요.',
  TOO_MANY_ATTEMPTS: '여러 번 틀려서 잠시 막혔어요. 1분 뒤에 다시 시도해주세요.',
}

const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-base text-text placeholder:text-field-placeholder md:text-[13px]'

interface DemoJoinModalProps {
  mode: DemoJoinMode
  onClose: () => void
}

export function DemoJoinModal({ mode, onClose }: DemoJoinModalProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [value, setValue] = useState('')
  const trimmed = value.trim()
  const isGuest = mode === 'guest'

  const joinMutation = useMutation({
    mutationFn: () => (isGuest ? joinAsGuest(trimmed) : joinAsAdmin(trimmed)),
    onSuccess: ({ accessToken }) => {
      setAccessToken(accessToken)
      // 같은 기기에서 다른 계정으로 바꿔 들어와도 이전 사용자 정보가 남지 않게 한다
      queryClient.removeQueries({ queryKey: ['me'] })
      navigate('/main')
    },
  })

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    if (!trimmed || joinMutation.isPending) return
    joinMutation.mutate()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
      <form
        role="dialog" aria-modal="true" aria-labelledby="demo-join-title"
        onSubmit={handleSubmit}
        className="flex w-full max-w-[400px] flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg"
      >
        <header className="flex items-center justify-between">
          <p id="demo-join-title" className="text-lg font-semibold text-text">
            {isGuest ? '닉네임으로 참가하기' : '어드민으로 참가하기'}
          </p>
          <button type="button" onClick={onClose} disabled={joinMutation.isPending} aria-label="닫기" className="text-base text-text-muted">
            ×
          </button>
        </header>

        <p className="text-xs text-text-muted">
          {isGuest ? '회원가입 없이 닉네임만으로 바로 들어갈 수 있어요. 닉네임은 방송에서 불릴 수 있어요.' : '발급받은 어드민 핀 번호를 입력해주세요.'}
        </p>

        <label className="flex flex-col gap-1.5">
          <span className="text-[13px] font-medium text-label">{isGuest ? '닉네임' : '핀 번호'}</span>
          <input
            type={isGuest ? 'text' : 'password'}
            inputMode={isGuest ? undefined : 'numeric'}
            autoComplete="off"
            autoFocus
            required
            maxLength={isGuest ? NICKNAME_MAX_LENGTH : PIN_MAX_LENGTH}
            value={value}
            onChange={(event) => { setValue(event.target.value); joinMutation.reset() }}
            disabled={joinMutation.isPending}
            placeholder={isGuest ? '예: 새벽청취자' : '숫자 4자리'}
            className={textInputClass}
          />
        </label>

        {joinMutation.isError && (
          <p role="alert" className="text-xs text-danger">
            {ERROR_TEXT[joinMutation.error.message] ?? '참가하지 못했어요. 잠시 후 다시 시도해주세요.'}
          </p>
        )}

        <footer className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={joinMutation.isPending}
            className="rounded-md px-4 py-2.5 text-[13px] font-medium text-text-muted"
          >
            취소
          </button>
          <button
            type="submit"
            disabled={!trimmed || joinMutation.isPending}
            className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
          >
            {joinMutation.isPending ? '참가하는 중...' : '참가하기'}
          </button>
        </footer>
      </form>
    </div>
  )
}
