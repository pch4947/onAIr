// 신청곡 입력 모달

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { getAccessToken } from '@/shared/authToken'
import { createTrackClient, normalizeYoutubeLink } from './trackClient'
import type { TrackCandidate } from './trackClient'

const client = createTrackClient(import.meta.env.VITE_API_BASE_URL ?? '', getAccessToken)

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder'

interface SongRequestModalProps {
  onClose: () => void
  stationId?: string
}

export function SongRequestModal({ onClose, stationId }: SongRequestModalProps) {
  const [link, setLink] = useState('')
  const [validation, setValidation] = useState('')
  const [candidates, setCandidates] = useState<TrackCandidate[] | null>(null)
  const [selected, setSelected] = useState<TrackCandidate | null>(null)
  const searchAbort = useRef<AbortController | null>(null)
  const searchVersion = useRef(0)
  const directLink = normalizeYoutubeLink(link)
  useEffect(() => () => { searchAbort.current?.abort() }, [])
  const retry = useRef<{ url: string; key: string } | null>(null)
  const submitting = useRef(false)
  const station = useQuery({
    queryKey: ['track-access', stationId], queryFn: () => client.checkStation(stationId!),
    enabled: Boolean(stationId), retry: false, staleTime: 0,
  })
  const submitSongRequestMutation = useMutation({
    mutationFn: ({ url, key }: { url: string; key: string }) => client.enqueue(stationId!, url, key), retry: false,
  })
  const searchMutation = useMutation({
    mutationFn: ({ query, signal }: { query: string; signal: AbortSignal }) => client.search(stationId!, query, signal),
    retry: false,
  })

  async function handleSearch() {
    if (!stationId || !station.data || !link.trim() || submitSongRequestMutation.isPending) return
    if (/^(https?:\/\/|www\.)/i.test(link.trim())) {
      setValidation('올바른 유튜브 영상 링크를 입력해주세요.'); return
    }
    if (link.trim().length > 120) { setValidation('검색어는 120자 이하로 입력해주세요.'); return }
    setValidation('')
    setCandidates(null)
    setSelected(null)
    searchAbort.current?.abort()
    const controller = new AbortController()
    searchAbort.current = controller
    const version = ++searchVersion.current
    try {
      const result = await searchMutation.mutateAsync({ query: link.trim(), signal: controller.signal })
      if (version === searchVersion.current) setCandidates(result.candidates)
    } catch { /* Display current search error below. */ }
  }

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (submitting.current || submitSongRequestMutation.isSuccess || !station.data || !stationId) return
    if (!directLink && !selected) { await handleSearch(); return }
    const url = directLink ?? selected!.url
    setValidation('')
    if (!retry.current || retry.current.url !== url) retry.current = { url, key: crypto.randomUUID() }
    submitting.current = true
    try { await submitSongRequestMutation.mutateAsync(retry.current) } catch { /* Display mutation error below. */ }
    finally { submitting.current = false }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
      <form
        role="dialog" aria-modal="true" aria-labelledby="song-request-title"
        onSubmit={handleSubmit}
        className="flex max-h-[90dvh] w-full max-w-[480px] flex-col gap-4 overflow-y-auto rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg"
      >
        <header className="flex items-center justify-between">
          <p id="song-request-title" className="text-lg font-semibold text-text">유튜브 음악 등록</p>
          <button type="button" onClick={onClose} disabled={submitSongRequestMutation.isPending} aria-label="닫기" className="text-base text-text-muted">
            ×
          </button>
        </header>

        <p className="text-xs text-text-muted">로그인한 청취자 누구나 신청할 수 있어요. 링크 또는 가수·곡 제목으로 입력해주세요. 검색은 10분 이하 일반 영상만 보여줘요.</p>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>유튜브 링크 또는 검색어</span>
          <input
            type="text"
            required maxLength={2048}
            disabled={submitSongRequestMutation.isPending || submitSongRequestMutation.isSuccess}
            value={link}
            onChange={(event) => {
              searchVersion.current++
              searchAbort.current?.abort()
              setLink(event.target.value); setCandidates(null); setSelected(null); setValidation('')
              searchMutation.reset(); submitSongRequestMutation.reset()
            }}
            placeholder="예: 아이유 밤편지 또는 https://youtu.be/..."
            className={textInputClass}
          />
        </label>

        {!directLink && <button type="button" onClick={handleSearch}
          disabled={!station.data || !link.trim() || searchMutation.isPending || submitSongRequestMutation.isPending || submitSongRequestMutation.isSuccess}
          className="rounded-md border border-field-border px-4 py-2 text-sm disabled:opacity-60">
          {searchMutation.isPending ? '검색 중...' : '검색'}
        </button>}
        {searchMutation.isPending && <p role="status" className="text-xs">후보 영상을 찾고 있어요...</p>}
        {searchMutation.error && <p role="alert" className="text-xs text-danger">검색하지 못했어요. 잠시 후 다시 검색해주세요.</p>}
        {candidates?.length === 0 && <p role="status" className="text-xs">조건에 맞는 영상이 없어요. 가수와 곡 제목을 바꿔 검색해주세요.</p>}
        {candidates && candidates.length > 0 && <fieldset disabled={submitSongRequestMutation.isPending || submitSongRequestMutation.isSuccess} className="flex flex-col gap-2">
          <legend className="mb-2 text-sm">검색 결과 · 재생할 영상을 선택해주세요</legend>
          {candidates.map((item) => <div key={item.video_id} className={`rounded-lg border p-3 ${selected?.video_id === item.video_id ? 'border-primary bg-primary/5' : 'border-field-border'}`}>
            <label className="flex cursor-pointer items-center gap-3">
              <img src={item.thumbnail_url} alt="" referrerPolicy="no-referrer" className="h-14 w-20 shrink-0 rounded object-cover" />
              <span className="min-w-0 flex-1">
                <span className="line-clamp-2 text-[13px] font-medium">{item.title}</span>
                <span className="block text-xs text-text-muted">{item.channel} · {Math.floor(item.duration_ms / 60000)}:{String(Math.floor(item.duration_ms / 1000) % 60).padStart(2, '0')}</span>
              </span>
              <input type="radio" name="track-candidate" aria-label={item.title}
                checked={selected?.video_id === item.video_id} onChange={() => { setSelected(item); submitSongRequestMutation.reset() }} />
            </label>
            <a href={item.url} target="_blank" rel="noopener noreferrer" className="mt-2 block text-right text-xs text-primary">유튜브에서 확인</a>
          </div>)}
          <p className="text-xs text-text-muted">선택 후 아래 버튼을 눌러 등록해주세요. 영상에 따라 다운로드가 제한될 수 있어요.</p>
        </fieldset>}

        {!stationId && <p role="alert">방송 정보를 불러온 뒤 다시 열어주세요.</p>}
        {stationId && station.isPending && <p role="status">방송 권한을 확인하고 있어요...</p>}
        {station.error && <p role="alert" className="text-xs text-danger">{station.error.message}</p>}
        {station.data === false && <p role="alert">방송을 시작한 뒤 등록해주세요.</p>}
        {validation && <p role="alert" className="text-xs text-danger">{validation}</p>}
        {submitSongRequestMutation.isPending && <p role="status" className="text-xs">음원을 다운로드하고 준비하고 있어요. 최대 4분 정도 걸릴 수 있어요.</p>}
        {submitSongRequestMutation.error && <p role="alert" className="text-xs text-danger">{submitSongRequestMutation.error.message}</p>}
        {submitSongRequestMutation.isSuccess && <p role="status" className="text-sm text-text">
          {submitSongRequestMutation.data.track.title} — 방송 대기열에 추가됐어요. 앞선 오디오 재생 후 들을 수 있어요.
        </p>}

        <footer className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitSongRequestMutation.isPending}
            className="rounded-md px-4 py-2.5 text-[13px] font-medium text-text-muted"
          >
            {submitSongRequestMutation.isSuccess ? '닫기' : '취소'}
          </button>
          {!submitSongRequestMutation.isSuccess && <button
            type="submit"
            disabled={submitSongRequestMutation.isPending || !station.data || (!directLink && !selected)}
            className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
          >
            {submitSongRequestMutation.isPending ? '음원 준비 중...' : submitSongRequestMutation.isError ? '다시 시도' : selected ? '선택한 곡 대기열에 추가' : '대기열에 추가'}
          </button>}
        </footer>
      </form>
    </div>
  )
}
