// FEATURE: 00b_Main_방송 시작하기 (모달)

import { useState } from 'react'

type Tone = '차분한' | '발랄한' | '따뜻한' | '재미있는'

const TONES: Tone[] = ['차분한', '발랄한', '따뜻한', '재미있는']

const DURATIONS = ['1시간', '2시간', '3시간']

interface StartBroadcastModalProps {
  onClose: () => void
}

export function StartBroadcastModal({ onClose }: StartBroadcastModalProps) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [tone, setTone] = useState<Tone>('차분한')
  const [playlistMode, setPlaylistMode] = useState('무드 기반 자동 선곡')
  const [tags, setTags] = useState(['#발라드', '#어쿠스틱', '#잔잔한'])
  const [startTime, setStartTime] = useState('20:00')
  const [duration, setDuration] = useState('2시간')

  const handleAddTag = () => {
    const nextTag = window.prompt('추가할 태그를 입력하세요')
    if (!nextTag) return
    setTags((prev) => [...prev, nextTag])
  }

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    onClose()
  }

  return (
    <div role="dialog" aria-modal="true">
      <form onSubmit={handleSubmit}>
        <header>
          <h2>방송 시작하기</h2>
          <button type="button" onClick={onClose}>
            ×
          </button>
        </header>

        <label>
          방송 제목
          <input
            type="text"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="예: 감성 발라드 나이트"
          />
        </label>

        <label>
          방송 설명 (해시태그)
          <input
            type="text"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="#감성발라드 #새벽감성 #잔잔한"
          />
        </label>

        <fieldset>
          <legend>AI 말투 톤</legend>
          {TONES.map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={tone === option}
              onClick={() => setTone(option)}
            >
              {option}
            </button>
          ))}
        </fieldset>

        <label>
          플레이리스트 설정
          <input
            type="text"
            value={playlistMode}
            onChange={(event) => setPlaylistMode(event.target.value)}
          />
        </label>
        <ul>
          {tags.map((tag) => (
            <li key={tag}>{tag}</li>
          ))}
          <li>
            <button type="button" onClick={handleAddTag}>
              + 태그 추가
            </button>
          </li>
        </ul>

        <div>
          <label>
            시작 시간
            <input
              type="time"
              value={startTime}
              onChange={(event) => setStartTime(event.target.value)}
            />
          </label>
          <label>
            방송 길이
            <select value={duration} onChange={(event) => setDuration(event.target.value)}>
              {DURATIONS.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
        </div>

        <footer>
          <button type="button" onClick={onClose}>
            취소
          </button>
          <button type="submit">방송 시작</button>
        </footer>
      </form>
    </div>
  )
}
