// 사연 보내기 모달 — DJ가 문자 소개 코너에서 닉네임과 함께 읽어 준다

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { submitRequest } from '@/shared/api'
import { isUnauthorizedError, LoginExpiredNotice } from '@/routes/listen/LoginExpiredNotice'

const MESSAGE_MAX_LENGTH = 200

interface MessageModalProps {
  stationId?: string
  listenerName?: string
  onClose: () => void
  onSubmitted: () => void
}

export function MessageModal({ stationId, listenerName, onClose, onSubmitted }: MessageModalProps) {
  const [body, setBody] = useState('')
  const [anonymous, setAnonymous] = useState(false)
  const sendMutation = useMutation({ mutationFn: submitRequest })
  const trimmed = body.trim()

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!trimmed || sendMutation.isPending) return
    try {
      await sendMutation.mutateAsync({
        stationId, prompt: trimmed, listenerName: anonymous ? undefined : listenerName, kind: 'message',
      })
      onSubmitted()
      onClose()
    } catch { /* Display mutation error below. */ }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
      <form
        role="dialog" aria-modal="true" aria-labelledby="message-title"
        onSubmit={handleSubmit}
        className="flex max-h-[90dvh] w-full max-w-[480px] flex-col gap-4 overflow-y-auto rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg"
      >
        <header className="flex items-center justify-between">
          <p id="message-title" className="text-lg font-semibold text-text">사연 보내기</p>
          <button type="button" onClick={onClose} disabled={sendMutation.isPending} aria-label="닫기" className="text-base text-text-muted">
            ×
          </button>
        </header>

        <p className="text-xs text-text-muted">
          보낸 사연은 DJ가 방송에서 읽어 줄 수 있어요. 익명을 선택하지 않으면 닉네임도 함께 불려요.
        </p>

        <label className="flex flex-col gap-1.5">
          <span className="text-[13px] font-medium text-label">사연</span>
          <textarea
            required
            maxLength={MESSAGE_MAX_LENGTH}
            value={body}
            onChange={(event) => { setBody(event.target.value); sendMutation.reset() }}
            disabled={sendMutation.isPending}
            placeholder="DJ에게 전하고 싶은 이야기를 적어주세요"
            className="h-32 w-full resize-none rounded-lg border border-field-border bg-field-bg px-3.5 py-3 text-base text-text placeholder:text-sm placeholder:text-field-placeholder md:text-[13px] md:placeholder:text-[13px]"
          />
          <span className="self-end text-[11px] text-text-muted">{body.length}/{MESSAGE_MAX_LENGTH}</span>
        </label>

        <label className="flex items-center gap-2 text-[13px] text-text">
          <input
            type="checkbox"
            checked={anonymous}
            onChange={(event) => setAnonymous(event.target.checked)}
            disabled={sendMutation.isPending}
          />
          익명으로 보내기
        </label>

        {sendMutation.isError && (isUnauthorizedError(sendMutation.error)
          ? <LoginExpiredNotice className="text-xs" />
          : <p role="alert" className="text-xs text-danger">사연을 보내지 못했어요, 다시 시도해주세요</p>
        )}

        <footer className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={sendMutation.isPending}
            className="rounded-md px-4 py-2.5 text-[13px] font-medium text-text-muted"
          >
            취소
          </button>
          <button
            type="submit"
            disabled={!trimmed || sendMutation.isPending}
            className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
          >
            {sendMutation.isPending ? '보내는 중...' : '보내기'}
          </button>
        </footer>
      </form>
    </div>
  )
}
