// 04_Board_게시판 화면

import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Sidebar } from '@/app/Sidebar'
import { LikeButton } from '@/routes/board/LikeButton'
import { NewPostModal } from '@/routes/board/NewPostModal'
import { PostDetailModal } from '@/routes/board/PostDetailModal'
import { getBoardPost, getBoardPosts, likePost, unlikePost } from '@/shared/api'
import type { BoardPost, LikeStatus } from '@/shared/types'

const PAGE_SIZE = 7

function formatTimeAgo(iso: string): string {
  const date = new Date(iso)
  const minutes = Math.floor((Date.now() - date.getTime()) / 60000)
  if (minutes < 1) return '방금 전'
  if (minutes < 60) return `${minutes}분 전`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}시간 전`
  return `${date.getMonth() + 1}/${date.getDate()}`
}

export function BoardPage() {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [selectedPost, setSelectedPost] = useState<BoardPost | null>(null)
  const [page, setPage] = useState(1)
  const postsQuery = useQuery({
    queryKey: ['board-posts', page],
    queryFn: () => getBoardPosts({ offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE }),
  })
  const posts = postsQuery.data?.posts ?? []
  const totalPages = Math.max(1, Math.ceil((postsQuery.data?.total ?? 0) / PAGE_SIZE))

  // 알림(예: 새 댓글)을 클릭해서 들어온 경우 — 목록에 없어도(다른 페이지에 있거나 오래된 글이어도) 바로 열어준다.
  const linkedPostId = searchParams.get('post')
  useEffect(() => {
    if (!linkedPostId) return
    let cancelled = false
    getBoardPost(linkedPostId).then((post) => {
      if (!cancelled) setSelectedPost(post)
    }).catch(() => {})
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('post')
      return next
    }, { replace: true })
    return () => {
      cancelled = true
    }
  }, [linkedPostId, setSearchParams])

  const updatePostLikeInCache = (postId: string, status: LikeStatus) => {
    queryClient.setQueryData<{ posts: BoardPost[]; total: number }>(['board-posts', page], (old) =>
      old ? { ...old, posts: old.posts.map((post) => (post.id === postId ? { ...post, ...status } : post)) } : old,
    )
  }

  const handleTogglePostLike = async (post: BoardPost) => {
    const status = post.liked ? await unlikePost(post.id) : await likePost(post.id)
    updatePostLikeInCache(post.id, status)
  }

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
          {posts.map((post) => (
            <li
              key={post.id}
              onClick={() => setSelectedPost(post)}
              className="flex cursor-pointer flex-col gap-2 rounded-md border border-[#e6e6e6] bg-surface px-4 py-3.5"
            >
              <p className="text-[15px] font-semibold text-text">{post.title}</p>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-semibold text-text">{post.authorName}</span>
                  <span className="text-[11px] text-[#999]">· {formatTimeAgo(post.createdAt)}</span>
                </div>
                <LikeButton
                  liked={post.liked}
                  count={post.likeCount}
                  onToggle={() => handleTogglePostLike(post)}
                />
              </div>
            </li>
          ))}
        </ul>

        <div className="mt-5 flex items-center justify-center gap-3">
          <button
            type="button"
            onClick={() => setPage((prev) => Math.max(1, prev - 1))}
            disabled={page <= 1}
            className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
          >
            이전
          </button>
          <span className="text-xs text-text-muted">
            {page} / {totalPages}
          </span>
          <button
            type="button"
            onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
            disabled={page >= totalPages}
            className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
          >
            다음
          </button>
        </div>
      </main>

      {isModalOpen && (
        <NewPostModal
          onClose={() => setIsModalOpen(false)}
          onCreated={() => {
            setIsModalOpen(false)
            // Changing the page re-triggers the query by itself; staying on page 1 needs an explicit refetch.
            if (page === 1) postsQuery.refetch()
            else setPage(1)
          }}
        />
      )}

      {selectedPost && (
        <PostDetailModal
          post={selectedPost}
          onClose={() => setSelectedPost(null)}
          onPostLikeChange={updatePostLikeInCache}
        />
      )}
    </div>
  )
}
