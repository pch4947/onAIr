// FEATURE: 05_Board_새 글 작성 (모달)

import { useState } from 'react'

interface NewPostModalProps {
  onClose: () => void
}

export function NewPostModal({ onClose }: NewPostModalProps) {
  const [story, setStory] = useState('')
  const [songRequest, setSongRequest] = useState('')

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    onClose()
  }

  return (
    <div role="dialog" aria-modal="true">
      <form onSubmit={handleSubmit}>
        <header>
          <h2>새 글 작성</h2>
          <button type="button" onClick={onClose}>
            ×
          </button>
        </header>

        <label>
          사연
          <textarea
            value={story}
            onChange={(event) => setStory(event.target.value)}
            placeholder="오늘 있었던 이야기, 하고 싶은 말을 자유롭게 적어주세요..."
          />
        </label>

        <label>
          🎵 듣고 싶은 곡 (선택)
          <input
            type="text"
            value={songRequest}
            onChange={(event) => setSongRequest(event.target.value)}
            placeholder="곡 제목이나 느낌을 적어주세요 (예: 잔잔한 발라드)"
          />
        </label>

        <footer>
          <button type="button" onClick={onClose}>
            취소
          </button>
          <button type="submit">등록</button>
        </footer>
      </form>
    </div>
  )
}
