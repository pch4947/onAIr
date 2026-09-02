// FEATURE: 04_Board_게시판 화면

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { NewPostModal } from '@/routes/board/NewPostModal'
import { BOARD_POSTS } from '@/routes/board/mock'

export function BoardPage() {
  const navigate = useNavigate()
  const [isModalOpen, setIsModalOpen] = useState(false)

  return (
    <div>
      <aside>
        <div>onAIr</div>
        <nav>
          <button type="button" onClick={() => navigate('/')}>
            홈 · 방송 선택
          </button>
          <button type="button" onClick={() => navigate('/admin')}>
            운영자 콘솔
          </button>
        </nav>
      </aside>

      <main>
        <header>
          <h1>게시판</h1>
          <p>사연과 듣고 싶은 곡을 자유롭게 남겨보세요</p>
        </header>

        <div>
          <input
            type="text"
            placeholder="지금 떠오르는 이야기나 듣고 싶은 곡을 자유롭게 남겨보세요"
            readOnly
            onClick={() => setIsModalOpen(true)}
          />
          <button type="button" onClick={() => setIsModalOpen(true)}>
            + 새 글 작성
          </button>
        </div>

        <ul>
          {BOARD_POSTS.map((post) => (
            <li key={post.id}>
              <p>{post.author}</p>
              <p>{post.content}</p>
              <span>{post.status}</span>
              {post.hasReplay && <button type="button">▶ 다시보기</button>}
            </li>
          ))}
        </ul>
      </main>

      {isModalOpen && <NewPostModal onClose={() => setIsModalOpen(false)} />}
    </div>
  )
}
