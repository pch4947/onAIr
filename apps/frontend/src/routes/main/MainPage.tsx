// 00_Main_방송 선택 화면

import { useState, useSyncExternalStore } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Sidebar } from '@/app/Sidebar'
import { ViewIcon } from '@/routes/listen/icons'
import { getMyStation } from '@/routes/station-create/api'
import { StationCreateModal } from '@/routes/station-create/StationCreateModal'
import { getStations } from '@/shared/api'
import { useCurrentUser } from '@/shared/useCurrentUser'
import type { StationFormat, StationListItem } from '@/shared/types'

const STATIONS_POLL_INTERVAL_MS = 10000
const FORMAT_LABEL: Record<StationFormat, string> = { talk: '토크', music: '음악', news: '뉴스' }

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
  station?: StationListItem
  onEnter?: () => void
}

// station이 없으면 자리만 차지하는 빈 카드 (마지막 페이지 높이 맞춤)
function StationCard({ station, onEnter }: StationCardProps) {
  const placeholder = !station
  const live = station?.status === 'started' && station.hls_ready
  const title = station?.topic || `${station?.dj_name ?? 'AI DJ'}의 방송`
  const tags = station ? [`#${FORMAT_LABEL[station.format] ?? station.format}`, station.dj_name ? `#DJ ${station.dj_name}` : null] : []
  return (
    <li
      aria-hidden={placeholder || undefined}
      className={`flex flex-col overflow-hidden rounded-lg border border-border bg-surface${placeholder ? ' invisible' : ''}`}
    >
      <div className="relative flex h-18 shrink-0 items-center justify-center bg-thumbnail md:h-35">
        <span className="text-3xl text-thumbnail-icon">♪</span>
        <span className={`absolute left-3 top-3 rounded-[10px] px-2 py-0.5 text-[9px] font-semibold text-white ${live ? 'bg-live' : 'bg-text-muted'}`}>
          {live ? 'LIVE' : '준비 중'}
        </span>
        {station?.mine && (
          <span className="absolute right-3 top-3 rounded-[10px] bg-primary px-2 py-0.5 text-[9px] font-semibold text-white">
            내 방송
          </span>
        )}
      </div>
      <div className="flex flex-col gap-1.5 px-4 pt-3.5">
        <h2 className="truncate text-sm font-semibold text-text">{title}</h2>
        <p className="truncate text-[11px] font-medium text-hashtag">
          {tags.filter(Boolean).join(' ')}
        </p>
        <p className="flex items-center gap-1 text-[11px] text-viewer">
          <ViewIcon className="size-3" />
          {station?.listeners ?? 0}명 시청 중
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
  // 게스트는 방송을 만들 수 없다 (서버도 막는다)
  const { data: currentUser } = useCurrentUser()
  const canCreate = Boolean(currentUser && !currentUser.isGuest)
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false)
  const [page, setPage] = useState(1)
  const stationsQuery = useQuery({
    queryKey: ['stations'],
    queryFn: getStations,
    enabled: Boolean(currentUser),
    refetchInterval: STATIONS_POLL_INTERVAL_MS,
  })
  const stations = stationsQuery.data?.stations ?? []
  // 진행 중인 내 방송이 있으면 새로 만들지 않고 그 방송으로 돌아간다 (한 사람당 하나)
  const myStationQuery = useQuery({
    queryKey: ['my-station'],
    queryFn: getMyStation,
    enabled: canCreate,
    refetchInterval: STATIONS_POLL_INTERVAL_MS,
  })
  const myStationId = myStationQuery.data?.station_id
  const handleHostButton = () => (myStationId ? navigate(`/listen/${myStationId}`) : setIsCreateModalOpen(true))
  const hostLabel = myStationId ? '내 방송으로 가기' : '방송 시작하기'
  const pageSize = useIsDesktop() ? DESKTOP_PAGE_SIZE : MOBILE_PAGE_SIZE
  const totalPages = Math.max(1, Math.ceil(stations.length / pageSize))
  // 화면 너비가 바뀌거나 방송이 끝나 페이지 수가 줄면 범위 안으로 맞춘다
  const currentPage = Math.min(page, totalPages)
  const pagedStations = stations.slice((currentPage - 1) * pageSize, currentPage * pageSize)

  return (
    <div className="flex min-h-screen">
      <Sidebar active="main" />

      <main className="flex flex-1 flex-col bg-bg p-4 pb-24 md:p-8 md:pb-8">
        <header className="mb-5 md:mb-6 md:flex md:items-center md:justify-between">
          <div className="flex flex-col gap-1">
            <h1 className="text-center text-lg font-semibold text-text md:text-left md:text-[22px]">방송 선택</h1>
            <p className="hidden text-[13px] text-text-muted md:block">지금 들을 수 있는 방송을 선택하세요</p>
          </div>
          {canCreate && (
            <button
              type="button"
              onClick={handleHostButton}
              className="hidden shrink-0 items-center gap-1.5 rounded-md bg-primary px-4.5 py-2.5 text-xs font-semibold text-white md:flex"
            >
              {hostLabel}
            </button>
          )}
        </header>

        {canCreate && (
          <div className="mb-5 flex items-center justify-end md:hidden">
            <button
              type="button"
              onClick={handleHostButton}
              className="flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-4.5 py-2.5 text-xs font-semibold text-white"
            >
              {hostLabel}
            </button>
          </div>
        )}

        {stationsQuery.isPending ? (
          <p className="py-16 text-center text-[13px] text-text-muted">방송 목록을 불러오는 중…</p>
        ) : stationsQuery.isError ? (
          <div className="flex flex-col items-center gap-3 py-16">
            <p className="text-[13px] text-danger">방송 목록을 불러오지 못했어요.</p>
            <button type="button" onClick={() => void stationsQuery.refetch()} className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted">
              다시 시도
            </button>
          </div>
        ) : stations.length === 0 ? (
          <p className="py-16 text-center text-[13px] text-text-muted">
            지금 진행 중인 방송이 없어요.{canCreate && ' 방송을 시작해 보세요.'}
          </p>
        ) : (
          <ul className="grid grid-cols-2 gap-3.5 md:grid-cols-[repeat(auto-fill,minmax(260px,1fr))] md:gap-5">
            {pagedStations.map((station) => (
              <StationCard key={station.station_id} station={station} onEnter={() => navigate(`/listen/${station.station_id}`)} />
            ))}
            {/* 마지막 페이지도 꽉 찬 페이지와 높이를 같게 해서 페이지네이션 위치를 고정한다 */}
            {totalPages > 1 && Array.from({ length: pageSize - pagedStations.length }, (_, index) => (
              <StationCard key={`placeholder-${index}`} />
            ))}
          </ul>
        )}

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
