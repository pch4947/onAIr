// 03_Admin_운영자 콘솔 화면

import { Sidebar } from '@/app/Sidebar'

export function AdminPage() {
  return (
    <div className="flex min-h-screen">
      <Sidebar active="admin" />
      <main className="flex-1 bg-bg" />
    </div>
  )
}
