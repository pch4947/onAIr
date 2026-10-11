// 05_Board_새 글 작성

import { useState } from 'react'
import { createBoardPost } from '@/shared/api'

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder'
const errorTextClass = 'block text-xs text-danger'

const URL_PATTERN = /^https?:\/\/\S+$/

interface NewPostModalProps {
  onClose: () => void
  onCreated: () => void
}

export function NewPostModal({ onClose, onCreated }: NewPostModalProps) {
  const [title, setTitle] = useState('')
  const [story, setStory] = useState('')
  const [songRequestUrl, setSongRequestUrl] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    const nextErrors: Record<string, string> = {}
    if (!title.trim()) nextErrors.title = '제목을 입력해주세요'
    if (!story.trim()) nextErrors.story = '사연을 입력해주세요'
    if (songRequestUrl.trim() && !URL_PATTERN.test(songRequestUrl.trim())) {
      nextErrors.songRequestUrl = 'http(s)로 시작하는 링크를 입력해주세요'
    }
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) return

    setSubmitError('')
    setIsSubmitting(true)
    try {
      await createBoardPost({
        title: title.trim(),
        body: story.trim(),
        songRequestUrl: songRequestUrl.trim() || undefined,
      })
      onCreated()
    } catch {
      setSubmitError('글을 올리지 못했어요, 잠시 후 다시 시도해주세요')
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-6">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-150 flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg"
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
          <span className={errorTextClass}>{errors.title || ' '}</span>
        </label>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>사연</span>
          <textarea
            value={story}
            onChange={(event) => setStory(event.target.value)}
            placeholder="오늘 있었던 이야기, 하고 싶은 말을 자유롭게 적어주세요..."
            className="h-45 w-full resize-none rounded-lg border border-field-border bg-field-bg px-3.5 py-3 text-[13px] text-text placeholder:text-field-placeholder"
          />
          <span className={errorTextClass}>{errors.story || ' '}</span>
        </label>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>🎵 듣고 싶은 곡 (선택, YouTube 링크)</span>
          <input
            type="text"
            value={songRequestUrl}
            onChange={(event) => setSongRequestUrl(event.target.value)}
            placeholder="https://youtube.com/watch?v=..."
            className={textInputClass}
          />
          <span className={errorTextClass}>{errors.songRequestUrl || ' '}</span>
        </label>

        {submitError && <p className="text-xs text-danger">{submitError}</p>}

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
            disabled={isSubmitting}
            className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
          >
            {isSubmitting ? '등록 중...' : '등록'}
          </button>
        </footer>
      </form>
    </div>
  )
}
