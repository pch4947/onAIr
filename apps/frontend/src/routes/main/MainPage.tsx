// 00_Main_방송 선택 화면

import { useState, useSyncExternalStore } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sidebar } from '@/app/Sidebar'
import { ViewIcon } from '@/routes/listen/icons'
import { StationCreateModal } from '@/routes/station-create/StationCreateModal'
import { STATIONS } from '@/routes/main/mock'
import type { Station } from '@/routes/main/mock'

const DESKTOP_PAGE_SIZE = 10
const MOBILE_PAGE_SIZE = 6
// Tailwind md 브레이크포인트와 같은 기준
const desktopQuery = window.matchMedia('(min-width: 48rem)')

function useIsDesktop() {
  return useSyncExternalStore(
    (onChange) => {
      desktopQuery.addEventListener('change', onChange)
      return () => desktopQuery.removeEventListener('change', onChange)
    },
    () => desktopQuery.matches,
  )
}

interface StationCardProps {
  station: Station
  onEnter?: () => void
  placeholder?: boolean
}

function StationCard({ station, onEnter, placeholder = false }: StationCardProps) {
  return (
    <li
      aria-hidden={placeholder || undefined}
      className={`flex flex-col overflow-hidden rounded-lg border border-border bg-surface${placeholder ? ' invisible' : ''}`}
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
          onClick={onEnter}
          tabIndex={placeholder ? -1 : undefined}
          className="w-full rounded-md bg-primary py-3 text-xs font-semibold text-white"
        >
          입장하기
        </button>
      </div>
    </li>
  )
}

export function MainPage() {
  const navigate = useNavigate()
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false)
  const [page, setPage] = useState(1)
  const pageSize = useIsDesktop() ? DESKTOP_PAGE_SIZE : MOBILE_PAGE_SIZE
  const totalPages = Math.max(1, Math.ceil(STATIONS.length / pageSize))
  // 화면 너비가 바뀌어 페이지 수가 줄면 범위 안으로 맞춘다
  const currentPage = Math.min(page, totalPages)
  const pagedStations = STATIONS.slice((currentPage - 1) * pageSize, currentPage * pageSize)

  return (
    <div className="flex min-h-screen">
      <Sidebar active="main" />

      <main className="flex flex-1 flex-col bg-bg p-4 pb-24 md:p-8 md:pb-8">
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
            <StationCard key={station.id} station={station} onEnter={() => navigate('/listen')} />
          ))}
          {/* 마지막 페이지도 꽉 찬 페이지와 높이를 같게 해서 페이지네이션 위치를 고정한다 */}
          {totalPages > 1 && Array.from({ length: pageSize - pagedStations.length }, (_, index) => (
            <StationCard key={`placeholder-${index}`} station={STATIONS[0]} placeholder />
          ))}
        </ul>

        {totalPages > 1 && (
          <div className="mt-auto flex items-center justify-center gap-3 pt-5">
            <button
              type="button"
              onClick={() => setPage(Math.max(1, currentPage - 1))}
              disabled={currentPage <= 1}
              className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
            >
              이전
            </button>
            <span className="text-xs text-text-muted">
              {currentPage} / {totalPages}
            </span>
            <button
              type="button"
              onClick={() => setPage(Math.min(totalPages, currentPage + 1))}
              disabled={currentPage >= totalPages}
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
