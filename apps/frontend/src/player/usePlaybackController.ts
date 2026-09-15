// FEATURE: mock 플레이리스트 재생 컨트롤 (재생/일시정지, 진행 시간, 트랙 종료 시 자동 다음곡 전환)

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Track } from '@/routes/listen/mock'

export function usePlaybackController(playlist: Track[]) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [currentTrackIndex, setCurrentTrackIndex] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [positionSec, setPositionSec] = useState(0)

  const currentTrack = playlist[currentTrackIndex]

  const isPlayingRef = useRef(isPlaying)
  useEffect(() => {
    isPlayingRef.current = isPlaying
  }, [isPlaying])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return

    const handleTimeUpdate = () => setPositionSec(audio.currentTime)
    const handleEnded = () => setCurrentTrackIndex((index) => (index + 1) % playlist.length)

    audio.addEventListener('timeupdate', handleTimeUpdate)
    audio.addEventListener('ended', handleEnded)
    return () => {
      audio.removeEventListener('timeupdate', handleTimeUpdate)
      audio.removeEventListener('ended', handleEnded)
    }
  }, [playlist.length])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return

    setPositionSec(0)
    audio.src = currentTrack.audioUrl
    audio.load()
    if (isPlayingRef.current) {
      void audio.play()
    }
  }, [currentTrack.audioUrl])

  const togglePlay = useCallback(() => {
    const audio = audioRef.current
    if (!audio) return

    if (isPlaying) {
      audio.pause()
      setIsPlaying(false)
    } else {
      void audio.play()
      setIsPlaying(true)
    }
  }, [isPlaying])

  return { audioRef, currentTrack, isPlaying, positionSec, togglePlay }
}
