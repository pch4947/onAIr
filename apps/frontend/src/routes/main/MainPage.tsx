// 00_Main_방송 선택 화면

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sidebar } from '@/app/Sidebar'
import { ViewIcon } from '@/routes/listen/icons'
import { StationCreateModal } from '@/routes/station-create/StationCreateModal'
import { STATIONS } from '@/routes/main/mock'

const PAGE_SIZE = 6

export function MainPage() {
  const navigate = useNavigate()
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false)
  const [page, setPage] = useState(1)
  const totalPages = Math.max(1, Math.ceil(STATIONS.length / PAGE_SIZE))
  const pagedStations = STATIONS.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  return (
    <div className="flex min-h-screen">
      <Sidebar active="main" />

      <main className="flex-1 bg-bg p-4 pb-24 md:p-8 md:pb-8">
        <header className="mb-5 md:mb-6 md:flex md:items-center md:justify-between">
          <div className="flex flex-col gap-1">
            <h1 className="text-center text-lg font-semibold text-text md:text-left md:text-[22px]">방송 선택</h1>
            <p className="hidden text-[13px] text-text-muted md:block">지금 들을 수 있는 방송을 선택하세요</p>
          </div>
          <button
            type="button"
            onClick={() => setIsCreateModalOpen(true)}
            className="hidden shrink-0 items-center gap-1.5 rounded-md bg-primary px-4.5 py-2.5 text-xs font-semibold text-white md:flex"
          >
            방송 시작하기
          </button>
        </header>

        <div className="mb-5 flex items-center justify-end md:hidden">
          <button
            type="button"
            onClick={() => setIsCreateModalOpen(true)}
            className="flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-4.5 py-2.5 text-xs font-semibold text-white"
          >
            방송 시작하기
          </button>
        </div>

        <ul className="grid grid-cols-2 gap-3.5 md:grid-cols-[repeat(auto-fill,minmax(260px,1fr))] md:gap-5">
          {pagedStations.map((station) => (
            <li
              key={station.id}
              className="flex flex-col overflow-hidden rounded-lg border border-border bg-surface"
            >
              <div className="relative flex h-18 shrink-0 items-center justify-center bg-thumbnail md:h-35">
                <span className="text-3xl text-thumbnail-icon">♪</span>
                <span className="absolute left-3 top-3 rounded-[10px] bg-live px-2 py-0.5 text-[9px] font-semibold text-white">
                  LIVE
                </span>
              </div>
              <div className="flex flex-col gap-1.5 px-4 pt-3.5">
                <h2 className="truncate text-sm font-semibold text-text">{station.title}</h2>
                <p className="truncate text-[11px] font-medium text-hashtag">
                  {station.hashtags.join(' ')}
                </p>
                <p className="flex items-center gap-1 text-[11px] text-viewer">
                  <ViewIcon className="size-3" />
                  {station.viewerCount}명 시청 중
                </p>
              </div>
              <div className="mt-auto p-4">
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

        {totalPages > 1 && (
          <div className="mt-5 flex items-center justify-center gap-3">
            <button
              type="button"
              onClick={() => setPage((prev) => Math.max(1, prev - 1))}
              disabled={page <= 1}
              className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
            >
              이전
            </button>
            <span className="text-xs text-text-muted">
              {page} / {totalPages}
            </span>
            <button
              type="button"
              onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
              disabled={page >= totalPages}
              className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
            >
              다음
            </button>
          </div>
        )}
      </main>

      {isCreateModalOpen && <StationCreateModal onClose={() => setIsCreateModalOpen(false)} />}
    </div>
  )
}
