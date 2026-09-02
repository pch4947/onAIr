// FEATURE: 01_Listen_스트리밍 방송 화면

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ExperimentSurveyModal } from '@/routes/listen/ExperimentSurveyModal'
import {
  CHAT_MESSAGES,
  CURRENT_TRACK,
  MY_REQUEST_STATUSES,
  STATION_INFO,
  SYNC_OFFSET_SEC,
} from '@/routes/listen/mock'

function formatTime(sec: number): string {
  const minutes = Math.floor(sec / 60)
  const seconds = Math.floor(sec % 60)
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

export function ListenPage() {
  const [isSurveyOpen, setIsSurveyOpen] = useState(false)
  const [chatInput, setChatInput] = useState('')
  const navigate = useNavigate()

  const handleSendChat = (event: React.FormEvent) => {
    event.preventDefault()
    setChatInput('')
  }

  return (
    <div>
      <aside>
        <div>onAIr</div>
        <nav>
          <button type="button" onClick={() => navigate('/')}>
            홈 · 방송 선택
          </button>
          <button type="button" onClick={() => navigate('/admin')}>
            운영자 콘솔
          </button>
        </nav>
      </aside>

      <main>
        <section>
          <header>
            <span>LIVE</span>
            <span>{STATION_INFO.name}</span>
            <span>{STATION_INFO.viewerCount}명 시청 중</span>
          </header>

          <div>
            <span>♪</span>
          </div>

          <div>
            <p>코너: {CURRENT_TRACK.cornerName}</p>
            <p>
              지금 재생 중 — {CURRENT_TRACK.title} · {CURRENT_TRACK.artist}
            </p>
            <progress value={CURRENT_TRACK.positionSec} max={CURRENT_TRACK.durationSec} />
            <span>
              {formatTime(CURRENT_TRACK.positionSec)} / {formatTime(CURRENT_TRACK.durationSec)}
            </span>
            <p>동기화 오차(E_sync): {SYNC_OFFSET_SEC}s</p>
          </div>

          <div>
            <h2>내 요청 상태</h2>
            <ul>
              {MY_REQUEST_STATUSES.map((item) => (
                <li key={item.id}>
                  {item.text}
                  {item.visibleToSelfOnly ? ' (본인에게만 표시)' : ''}
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section>
          <h2>채팅 · 사연</h2>
          <ul>
            {CHAT_MESSAGES.map((message) => (
              <li key={message.id}>
                {message.author}: {message.text}
              </li>
            ))}
          </ul>
          <form onSubmit={handleSendChat}>
            <input
              type="text"
              value={chatInput}
              onChange={(event) => setChatInput(event.target.value)}
            />
            <button type="submit">전송</button>
            <button type="button">음성 사연</button>
          </form>
        </section>
      </main>

      <button type="button" onClick={() => setIsSurveyOpen(true)}>
        실험 참여
      </button>

      {isSurveyOpen && <ExperimentSurveyModal onClose={() => setIsSurveyOpen(false)} />}
    </div>
  )
}
