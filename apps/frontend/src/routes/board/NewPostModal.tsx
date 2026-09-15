// 05_Board_새 글 작성

import { useState } from 'react'

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder'

interface NewPostModalProps {
  onClose: () => void
}

export function NewPostModal({ onClose }: NewPostModalProps) {
  const [title, setTitle] = useState('')
  const [story, setStory] = useState('')
  const [songRequest, setSongRequest] = useState('')

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    onClose()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-6">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-[480px] flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg"
      >
        <header className="flex items-center justify-between">
          <p className="text-lg font-semibold text-text">새 글 작성</p>
          <button type="button" onClick={onClose} className="text-base text-text-muted">
            ×
          </button>
        </header>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>제목</span>
          <input
            type="text"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="예: 오늘 하루 위로가 필요해요"
            className={textInputClass}
          />
        </label>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>사연</span>
          <textarea
            value={story}
            onChange={(event) => setStory(event.target.value)}
            placeholder="오늘 있었던 이야기, 하고 싶은 말을 자유롭게 적어주세요..."
            className="h-[180px] w-full resize-none rounded-lg border border-field-border bg-field-bg px-3.5 py-3 text-[13px] text-text placeholder:text-field-placeholder"
          />
        </label>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>🎵 듣고 싶은 곡 (선택)</span>
          <input
            type="text"
            value={songRequest}
            onChange={(event) => setSongRequest(event.target.value)}
            placeholder="곡 제목이나 느낌을 적어주세요 (예: 잔잔한 발라드)"
            className={textInputClass}
          />
        </label>

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
            className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white"
          >
            등록
          </button>
        </footer>
      </form>
    </div>
  )
}
