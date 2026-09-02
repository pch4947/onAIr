// FEATURE: 00_Main_방송 선택 화면

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { StartBroadcastModal } from '@/routes/main/StartBroadcastModal'
import { STATIONS } from '@/routes/main/mock'

export function MainPage() {
  const [isModalOpen, setIsModalOpen] = useState(false)
  const navigate = useNavigate()

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
        <header>
          <div>
            <h1>방송 선택</h1>
            <p>지금 들을 수 있는 방송을 선택하세요</p>
          </div>
          <button type="button" onClick={() => setIsModalOpen(true)}>
            방송 시작하기
          </button>
        </header>

        <ul>
          {STATIONS.map((station) => (
            <li key={station.id}>
              <div>
                <span>LIVE</span>
              </div>
              <h2>{station.title}</h2>
              <p>{station.hashtags.join(' ')}</p>
              <p>{station.viewerCount}명 시청 중</p>
              <button type="button" onClick={() => navigate('/listen')}>
                입장하기
              </button>
              <button type="button" onClick={() => navigate('/board')}>
                게시판
              </button>
            </li>
          ))}
        </ul>
      </main>

      {isModalOpen && <StartBroadcastModal onClose={() => setIsModalOpen(false)} />}
    </div>
  )
}
