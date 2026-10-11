import { getAccessToken } from '@/shared/authToken'
import { createStationClient } from './client'

export const { getVoices, generatePersonaDrafts, checkPersonaDraft, createStation, getStation } =
  createStationClient(import.meta.env.VITE_API_BASE_URL ?? '', getAccessToken)

// Team's provisional genre choices; the shared schema currently accepts strings.
export const GENRE_OPTIONS = ['발라드', '팝', '댄스/EDM', '힙합/랩', 'R&B/소울',
  '록/밴드', '어쿠스틱/포크', '인디', '재즈', '트로트'] as const
export const MAX_MUSIC_TASTE = 3
