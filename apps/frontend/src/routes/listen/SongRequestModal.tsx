// 신청곡 입력 모달

import { useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { getAccessToken } from '@/shared/authToken'
import { createTrackClient, normalizeYoutubeLink } from './trackClient'

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
  const retry = useRef<{ url: string; key: string } | null>(null)
  const submitting = useRef(false)
  const station = useQuery({
    queryKey: ['track-owner', stationId], queryFn: () => client.checkStation(stationId!),
    enabled: Boolean(stationId), retry: false, staleTime: 0,
  })
  const submitSongRequestMutation = useMutation({
    mutationFn: ({ url, key }: { url: string; key: string }) => client.enqueue(stationId!, url, key), retry: false,
  })

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (submitting.current || submitSongRequestMutation.isSuccess || !station.data || !stationId) return
    const url = normalizeYoutubeLink(link)
    if (!url) { setValidation('올바른 HTTPS 유튜브 영상 링크를 입력해주세요.'); return }
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
        className="flex w-full max-w-[420px] flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg"
      >
        <header className="flex items-center justify-between">
          <p id="song-request-title" className="text-lg font-semibold text-text">유튜브 음악 등록</p>
          <button type="button" onClick={onClose} disabled={submitSongRequestMutation.isPending} aria-label="닫기" className="text-base text-text-muted">
            ×
          </button>
        </header>

        <p className="text-xs text-text-muted">방송을 만든 계정 전용이에요. 10분 이하의 일반 영상 링크를 입력해주세요.</p>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>유튜브 링크</span>
          <input
            type="url"
            required maxLength={2048}
            disabled={submitSongRequestMutation.isPending || submitSongRequestMutation.isSuccess}
            value={link}
            onChange={(event) => { setLink(event.target.value); setValidation(''); submitSongRequestMutation.reset() }}
            placeholder="https://www.youtube.com/watch?v=..."
            className={textInputClass}
          />
        </label>

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
            disabled={submitSongRequestMutation.isPending || !station.data || !link.trim()}
            className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
          >
            {submitSongRequestMutation.isPending ? '음원 준비 중...' : submitSongRequestMutation.isError ? '다시 시도' : '대기열에 추가'}
          </button>}
        </footer>
      </form>
    </div>
  )
}
