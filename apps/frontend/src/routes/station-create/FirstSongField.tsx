// 방송 만들기의 첫 노래 입력 — 링크를 붙이거나, 신청곡 창처럼 검색해서 고른다 (#346)

import { useEffect, useRef, useState } from 'react'
import { normalizeYoutubeLink } from '@/routes/listen/trackClient'
import type { TrackCandidate } from '@/routes/listen/trackClient'
import { searchFirstSong } from '@/routes/station-create/api'
import { LOOKS_LIKE_LINK } from '@/routes/station-create/firstSong'
import type { FirstSongValue } from '@/routes/station-create/firstSong'

const QUERY_MAX_LENGTH = 120

function duration(ms: number) {
  return `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`
}

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full min-w-0 rounded-lg border border-field-border bg-field-bg px-3.5 text-base text-text placeholder:text-field-placeholder md:text-[13px]'

interface FirstSongFieldProps {
  value: FirstSongValue
  onChange: (value: FirstSongValue) => void
  error?: string
}

export function FirstSongField({ value, onChange, error }: FirstSongFieldProps) {
  const [candidates, setCandidates] = useState<TrackCandidate[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [notice, setNotice] = useState('')
  const abort = useRef<AbortController | null>(null)
  const version = useRef(0)
  useEffect(() => () => { abort.current?.abort() }, [])

  const query = value.text.trim()
  const directLink = normalizeYoutubeLink(value.text)

  const handleText = (text: string) => {
    // 검색어를 고치면 앞선 검색과 선택은 버린다
    version.current++
    abort.current?.abort()
    setCandidates(null)
    setSearching(false)
    setNotice('')
    onChange({ text, selected: null })
  }

  const handleSearch = async () => {
    if (!query || directLink || searching) return
    if (LOOKS_LIKE_LINK.test(query)) { setNotice('올바른 유튜브 영상 링크를 입력해주세요'); return }
    if (query.length > QUERY_MAX_LENGTH) { setNotice(`검색어는 ${QUERY_MAX_LENGTH}자 이하로 입력해주세요`); return }
    abort.current?.abort()
    const controller = new AbortController()
    abort.current = controller
    const current = ++version.current
    setNotice('')
    setCandidates(null)
    setSearching(true)
    onChange({ text: value.text, selected: null })
    try {
      const found = await searchFirstSong(query, controller.signal)
      if (current === version.current) setCandidates(found)
    } catch {
      if (current === version.current) setNotice('검색하지 못했어요. 잠시 후 다시 검색하거나 링크를 붙여 넣어주세요.')
    } finally {
      if (current === version.current) setSearching(false)
    }
  }

  const message = error || notice

  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <label htmlFor="first-song" className={fieldLabelClass}>첫 노래 (선택, YouTube 링크 또는 검색어)</label>
      <div className="flex gap-2">
        <input
          id="first-song"
          type="text"
          maxLength={2048}
          value={value.text}
          onChange={(event) => handleText(event.target.value)}
          onKeyDown={(event) => {
            // 엔터로 방송 만들기가 제출되지 않게 하고, 검색어면 검색한다
            if (event.key !== 'Enter' || event.nativeEvent.isComposing) return
            event.preventDefault()
            void handleSearch()
          }}
          placeholder="예: 아이유 밤편지 또는 https://youtu.be/..."
          className={textInputClass}
        />
        {!directLink && (
          <button
            type="button"
            onClick={() => void handleSearch()}
            disabled={!query || searching}
            className="h-10 shrink-0 rounded-lg border border-field-border px-4 text-[13px] font-medium text-text disabled:opacity-60"
          >
            {searching ? '검색 중...' : '검색'}
          </button>
        )}
      </div>

      {candidates?.length === 0 && (
        <p role="status" className="text-xs text-text-muted">
          조건에 맞는 영상이 없어요. 가수와 곡 제목을 바꿔 검색해주세요.
        </p>
      )}
      {candidates && candidates.length > 0 && (
        // fieldset은 기본 최소 너비가 내용 폭이라, min-w-0이 없으면 긴 제목이 창 밖으로 밀어낸다
        <fieldset className="flex min-w-0 flex-col gap-2">
          <legend className="mb-2 text-xs text-text-muted">검색 결과 · 첫 노래로 쓸 영상을 골라주세요</legend>
          {candidates.map((item) => {
            const checked = value.selected?.video_id === item.video_id
            return (
              <div
                key={item.video_id}
                className={`rounded-lg border p-3 ${checked ? 'border-primary bg-primary/5' : 'border-field-border'}`}
              >
                <label className="flex cursor-pointer items-center gap-3">
                  <img
                    src={item.thumbnail_url}
                    alt=""
                    referrerPolicy="no-referrer"
                    className="h-14 w-20 shrink-0 rounded object-cover"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="line-clamp-2 text-[13px] font-medium text-text">{item.title}</span>
                    <span className="flex text-xs text-text-muted">
                      <span className="truncate">{item.channel}</span>
                      <span className="shrink-0 whitespace-pre"> · {duration(item.duration_ms)}</span>
                    </span>
                  </span>
                  <input
                    type="radio"
                    name="first-song-candidate"
                    aria-label={item.title}
                    checked={checked}
                    onChange={() => onChange({ text: value.text, selected: item })}
                  />
                </label>
                <a
                  href={item.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-2 block text-right text-xs text-primary"
                >
                  유튜브에서 확인
                </a>
              </div>
            )
          })}
        </fieldset>
      )}

      <span role={message ? 'alert' : undefined} className={message ? 'block text-xs text-danger' : 'block text-xs text-text-muted'}>
        {message || (value.selected
          ? '방송이 시작되면 고른 곡이 먼저 나오고, DJ가 오프닝에서 소개해요.'
          : '방송이 시작되면 이 곡이 먼저 나오고, DJ가 오프닝에서 소개해요. 10분 이하 영상만 쓸 수 있어요.')}
      </span>
    </div>
  )
}
