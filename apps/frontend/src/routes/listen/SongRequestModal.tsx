// 신청곡 입력 모달

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { submitRequest } from '@/shared/api'

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder'

interface SongRequestModalProps {
  onClose: () => void
  onSubmitted: (text: string) => void
}

export function SongRequestModal({ onClose, onSubmitted }: SongRequestModalProps) {
  const [link, setLink] = useState('')
  const submitSongRequestMutation = useMutation({ mutationFn: submitRequest })

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    const trimmedLink = link.trim()
    if (!trimmedLink) return

    const prompt = `신청곡: ${trimmedLink}`

    submitSongRequestMutation.mutate(prompt, {
      onSuccess: () => {
        onSubmitted(prompt)
        onClose()
      },
    })
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-[420px] flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg"
      >
        <header className="flex items-center justify-between">
          <p className="text-lg font-semibold text-text">신청곡</p>
          <button type="button" onClick={onClose} className="text-base text-text-muted">
            ×
          </button>
        </header>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>유튜브 링크</span>
          <input
            type="url"
            value={link}
            onChange={(event) => setLink(event.target.value)}
            placeholder="https://www.youtube.com/watch?v=..."
            className={textInputClass}
          />
        </label>

        {submitSongRequestMutation.isError && (
          <p className="text-xs text-danger">전송에 실패했어요, 다시 시도해주세요</p>
        )}

        <footer className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-4 py-2.5 text-[13px] font-medium text-text-muted"
          >
            취소
          </button>
          <button
            type="submit"
            disabled={submitSongRequestMutation.isPending}
            className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
          >
            {submitSongRequestMutation.isPending ? '전송 중...' : '신청하기'}
          </button>
        </footer>
      </form>
    </div>
  )
}
