import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { MainPage } from '@/routes/main/MainPage'
import { ListenPage } from '@/routes/listen/ListenPage'
import { AdminPage } from '@/routes/admin/AdminPage'
import { BoardPage } from '@/routes/board/BoardPage'
import { MyPagePage } from '@/routes/mypage/MyPagePage'

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<MainPage />} />
        <Route path="/listen" element={<ListenPage />} />
        <Route path="/admin" element={<AdminPage />} />
        <Route path="/board" element={<BoardPage />} />
        <Route path="/mypage" element={<MyPagePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
