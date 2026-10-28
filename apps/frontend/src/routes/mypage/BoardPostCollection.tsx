import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { HeartIcon } from '@/routes/board/LikeButton'
import { PostDetailModal } from '@/routes/board/PostDetailModal'
import type { BoardPost, LikeStatus } from '@/shared/types'

const PAGE_SIZE = 10

interface BoardPostCollectionProps {
  queryKey: string
  fetcher: (params: { offset?: number; limit?: number }) => Promise<{ posts: BoardPost[]; total: number }>
  emptyText: string
  removeOnUnlike?: boolean
}

export function BoardPostCollection({ queryKey, fetcher, emptyText, removeOnUnlike }: BoardPostCollectionProps) {
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [selectedPost, setSelectedPost] = useState<BoardPost | null>(null)
  const query = useQuery({
    queryKey: [queryKey, page],
    queryFn: () => fetcher({ offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE }),
  })
  const posts = query.data?.posts ?? []
  const totalPages = Math.max(1, Math.ceil((query.data?.total ?? 0) / PAGE_SIZE))

  const handlePostLikeChange = (postId: string, status: LikeStatus) => {
    queryClient.setQueryData<{ posts: BoardPost[]; total: number }>([queryKey, page], (old) => {
      if (!old) return old
      if (removeOnUnlike && !status.liked) {
        return { ...old, posts: old.posts.filter((post) => post.id !== postId), total: old.total - 1 }
      }
      return { ...old, posts: old.posts.map((post) => (post.id === postId ? { ...post, ...status } : post)) }
    })
  }

  const handlePostDeleted = (postId: string) => {
    queryClient.setQueryData<{ posts: BoardPost[]; total: number }>([queryKey, page], (old) =>
      old ? { ...old, posts: old.posts.filter((post) => post.id !== postId), total: old.total - 1 } : old,
    )
  }

  if (!query.isLoading && posts.length === 0) {
    return <p className="text-[13px] text-text-muted">{emptyText}</p>
  }

  return (
    <div className="flex flex-col gap-4">
      <ul className="flex flex-col gap-2.5">
        {posts.map((post) => (
          <li
            key={post.id}
            onClick={() => setSelectedPost(post)}
            className="flex cursor-pointer flex-col gap-1 rounded-md border border-field-border bg-field-bg px-4 py-3.5"
          >
            <p className="text-sm font-semibold text-[#1a1a1f]">{post.title}</p>
            <p className="flex items-center gap-1 text-xs text-text-muted">
              {post.authorName} ·
              <HeartIcon className={`size-3 ${post.liked ? 'text-live' : ''}`} />
              {post.likeCount}
            </p>
          </li>
        ))}
      </ul>

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-3">
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
      )}

      {selectedPost && (
        <PostDetailModal
          post={selectedPost}
          onClose={() => setSelectedPost(null)}
          onPostLikeChange={handlePostLikeChange}
          onPostDeleted={handlePostDeleted}
        />
      )}
    </div>
  )
}
