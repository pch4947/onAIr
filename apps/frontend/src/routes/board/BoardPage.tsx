// 04_Board_게시판 화면

import { useState } from 'react'
import { Sidebar } from '@/app/Sidebar'
import { NewPostModal } from '@/routes/board/NewPostModal'
import { BOARD_POSTS } from '@/routes/board/mock'

export function BoardPage() {
  const [isModalOpen, setIsModalOpen] = useState(false)

  return (
    <div className="flex min-h-screen">
      <Sidebar active="board" />

      <main className="flex-1 bg-bg p-8">
        <header className="mb-5 flex flex-col gap-1">
          <h1 className="text-[22px] font-semibold text-text">게시판</h1>
          <p className="text-[13px] text-text-muted">사연과 듣고 싶은 곡을 자유롭게 남겨보세요</p>
        </header>

        <div className="mb-5 flex h-16 items-center justify-between rounded-md border border-border bg-surface p-5">
          <input
            type="text"
            placeholder="지금 떠오르는 이야기나 듣고 싶은 곡을 자유롭게 남겨보세요"
            readOnly
            onClick={() => setIsModalOpen(true)}
            className="flex-1 bg-transparent text-[13px] text-text-muted placeholder:text-text-muted"
          />
          <button
            type="button"
            onClick={() => setIsModalOpen(true)}
            className="flex shrink-0 items-center gap-1.5 rounded-md bg-primary px-4.5 py-2.5 text-xs font-semibold text-white"
          >
            + 새 글 작성
          </button>
        </div>

        <ul className="flex flex-col gap-2.5">
          {BOARD_POSTS.map((post) => (
            <li
              key={post.id}
              className="flex flex-col gap-2 rounded-md border border-[#e6e6e6] bg-surface px-4 py-3.5"
            >
              <p className="text-[15px] font-semibold text-text">{post.title}</p>
              <div className="flex items-center gap-2">
                <span className="text-xs font-semibold text-text">{post.author}</span>
                <span className="text-[11px] text-[#999]">· {post.timeAgo}</span>
              </div>
            </li>
          ))}
        </ul>
      </main>

      {isModalOpen && <NewPostModal onClose={() => setIsModalOpen(false)} />}
    </div>
  )
}
