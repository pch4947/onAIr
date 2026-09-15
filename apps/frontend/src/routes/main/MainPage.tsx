// 00_Main_방송 선택 화면

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sidebar } from '@/app/Sidebar'
import { StartBroadcastModal } from '@/routes/main/StartBroadcastModal'
import { STATIONS } from '@/routes/main/mock'

export function MainPage() {
  const [isModalOpen, setIsModalOpen] = useState(false)
  const navigate = useNavigate()

  return (
    <div className="flex min-h-screen">
      <Sidebar active="main" />

      <main className="flex-1 bg-bg p-8">
        <header className="mb-6 flex items-center justify-between">
          <div className="flex flex-col gap-1">
            <h1 className="text-[22px] font-semibold text-text">방송 선택</h1>
            <p className="text-[13px] text-text-muted">지금 들을 수 있는 방송을 선택하세요</p>
          </div>
          <button
            type="button"
            onClick={() => setIsModalOpen(true)}
            className="rounded-md bg-primary px-4.5 py-3 text-xs font-semibold text-white"
          >
            🎙 방송 시작하기
          </button>
        </header>

        <ul className="flex flex-wrap gap-5">
          {STATIONS.map((station) => (
            <li
              key={station.id}
              className="w-[286px] overflow-hidden rounded-lg border border-border bg-surface"
            >
              <div className="relative flex h-[140px] items-center justify-center bg-thumbnail">
                <span className="text-3xl text-thumbnail-icon">♪</span>
                <span className="absolute left-3 top-3 rounded-[10px] bg-live px-2 py-0.5 text-[9px] font-semibold text-white">
                  LIVE
                </span>
              </div>
              <div className="flex flex-col gap-1.5 px-4 pt-3.5">
                <h2 className="text-sm font-semibold text-text">{station.title}</h2>
                <p className="text-[11px] font-medium text-hashtag">
                  {station.hashtags.join(' ')}
                </p>
                <p className="text-[11px] text-viewer">👁 {station.viewerCount}명 시청 중</p>
              </div>
              <div className="p-4">
                <button
                  type="button"
                  onClick={() => navigate('/listen')}
                  className="w-full rounded-md bg-primary py-3 text-xs font-semibold text-white"
                >
                  입장하기
                </button>
              </div>
            </li>
          ))}
        </ul>
      </main>

      {isModalOpen && <StartBroadcastModal onClose={() => setIsModalOpen(false)} />}
    </div>
  )
}
