// 06_Board_게시글 상세보기

import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { LikeButton } from '@/routes/board/LikeButton'
import {
  createComment, deleteBoardPost, deleteComment, getComments, likeComment, likePost,
  unlikeComment, unlikePost,
} from '@/shared/api'
import { useCurrentUser } from '@/shared/useCurrentUser'
import type { BoardComment, BoardPost, LikeStatus } from '@/shared/types'

const COMMENT_PAGE_SIZE = 10

function formatDateTime(iso: string): string {
  const date = new Date(iso)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}.${pad(date.getMonth() + 1)}.${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

interface PostDetailModalProps {
  post: BoardPost
  onClose: () => void
  onPostLikeChange?: (postId: string, status: LikeStatus) => void
  onPostDeleted?: (postId: string) => void
}

export function PostDetailModal({ post, onClose, onPostLikeChange, onPostDeleted }: PostDetailModalProps) {
  const { data: currentUser } = useCurrentUser()
  const [postLike, setPostLike] = useState<LikeStatus>({ likeCount: post.likeCount, liked: post.liked })
  const [commentPage, setCommentPage] = useState(1)
  const [comments, setComments] = useState<BoardComment[]>([])
  const [commentTotal, setCommentTotal] = useState(0)
  const [isLoadingComments, setIsLoadingComments] = useState(false)
  const [commentBody, setCommentBody] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [isConfirmingPostDelete, setIsConfirmingPostDelete] = useState(false)
  const [commentDeleteTarget, setCommentDeleteTarget] = useState<BoardComment | null>(null)
  const commentTotalPages = Math.max(1, Math.ceil(commentTotal / COMMENT_PAGE_SIZE))

  const deletePostMutation = useMutation({
    mutationFn: () => deleteBoardPost(post.id),
    onSuccess: () => {
      onPostDeleted?.(post.id)
      onClose()
    },
  })

  const deleteCommentMutation = useMutation({
    mutationFn: (comment: BoardComment) => deleteComment(post.id, comment.id),
    onSuccess: (_, comment) => {
      setComments((prev) => prev.filter((item) => item.id !== comment.id))
      setCommentTotal((prev) => Math.max(0, prev - 1))
      setCommentDeleteTarget(null)
    },
  })

  const handleTogglePostLike = async () => {
    const status = postLike.liked ? await unlikePost(post.id) : await likePost(post.id)
    setPostLike(status)
    onPostLikeChange?.(post.id, status)
  }

  const handleToggleCommentLike = async (comment: BoardComment) => {
    const status = comment.liked ? await unlikeComment(comment.id) : await likeComment(comment.id)
    setComments((prev) => prev.map((item) => (item.id === comment.id ? { ...item, ...status } : item)))
  }

  const loadComments = async (page: number) => {
    setIsLoadingComments(true)
    try {
      const result = await getComments(post.id, { offset: (page - 1) * COMMENT_PAGE_SIZE, limit: COMMENT_PAGE_SIZE })
      setComments(result.comments)
      setCommentTotal(result.total)
    } finally {
      setIsLoadingComments(false)
    }
  }

  useEffect(() => {
    loadComments(1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [post.id])

  const handleCommentPageChange = (nextPage: number) => {
    setCommentPage(nextPage)
    loadComments(nextPage)
  }

  const handleSubmitComment = async (event: React.FormEvent) => {
    event.preventDefault()
    const trimmed = commentBody.trim()
    if (!trimmed) return
    setIsSubmitting(true)
    try {
      await createComment(post.id, trimmed)
      setCommentBody('')
      // New comments land at the end (chronological order), so jump to the new last page.
      const lastPage = Math.max(1, Math.ceil((commentTotal + 1) / COMMENT_PAGE_SIZE))
      setCommentPage(lastPage)
      await loadComments(lastPage)
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-6">
      <div className="flex w-full max-w-150 flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg">
        <header className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1">
            <p className="text-lg font-semibold text-text">{post.title}</p>
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold text-text-muted">{post.authorName}</span>
              <span className="text-[11px] text-[#999]">· {formatDateTime(post.createdAt)}</span>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <LikeButton liked={postLike.liked} count={postLike.likeCount} onToggle={handleTogglePostLike} />
            {post.authorId === currentUser?.id && (
              <button
                type="button"
                onClick={() => setIsConfirmingPostDelete(true)}
                className="text-xs font-medium text-text-muted hover:text-danger"
              >
                삭제
              </button>
            )}
            <button type="button" onClick={onClose} className="text-base text-text-muted">
              ×
            </button>
          </div>
        </header>

        <p className="min-h-15 whitespace-pre-wrap text-[13px] leading-relaxed text-text">{post.body}</p>

        {post.songRequestUrl && (
          <a
            href={post.songRequestUrl}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1.5 self-start rounded-md bg-field-bg px-3.5 py-2.5 text-[13px] font-medium text-primary"
          >
            🎵 듣고 싶은 곡 링크 열기
          </a>
        )}

        <div className="flex flex-col gap-2 border-t border-[#e6e6e6] pt-3">
          <p className="text-xs font-semibold text-text-muted">댓글 {commentTotal}</p>

          <ul className="flex flex-col gap-2">
            {comments.map((comment) => (
              <li key={comment.id} className="rounded-md bg-field-bg px-3 py-2">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-semibold text-text">{comment.authorName}</span>
                    <span className="text-[11px] text-[#999]">· {formatDateTime(comment.createdAt)}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <LikeButton
                      liked={comment.liked}
                      count={comment.likeCount}
                      onToggle={() => handleToggleCommentLike(comment)}
                    />
                    {comment.authorId === currentUser?.id && (
                      <button
                        type="button"
                        onClick={() => setCommentDeleteTarget(comment)}
                        className="text-[11px] font-medium text-text-muted hover:text-danger"
                      >
                        삭제
                      </button>
                    )}
                  </div>
                </div>
                <p className="mt-1 text-[13px] text-text">{comment.body}</p>
              </li>
            ))}
          </ul>

          {commentTotalPages > 1 && (
            <div className="flex items-center justify-center gap-3">
              <button
                type="button"
                onClick={() => handleCommentPageChange(Math.max(1, commentPage - 1))}
                disabled={commentPage <= 1 || isLoadingComments}
                className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
              >
                이전
              </button>
              <span className="text-xs text-text-muted">
                {commentPage} / {commentTotalPages}
              </span>
              <button
                type="button"
                onClick={() => handleCommentPageChange(Math.min(commentTotalPages, commentPage + 1))}
                disabled={commentPage >= commentTotalPages || isLoadingComments}
                className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
              >
                다음
              </button>
            </div>
          )}

          <form onSubmit={handleSubmitComment} className="flex gap-2">
            <input
              type="text"
              value={commentBody}
              onChange={(event) => setCommentBody(event.target.value)}
              placeholder="댓글을 남겨보세요"
              maxLength={500}
              className="h-9 flex-1 rounded-lg border border-field-border bg-field-bg px-3 text-[13px] text-text placeholder:text-field-placeholder"
            />
            <button
              type="submit"
              disabled={isSubmitting || !commentBody.trim()}
              className="shrink-0 rounded-md bg-primary px-3.5 py-2 text-xs font-semibold text-white disabled:opacity-60"
            >
              등록
            </button>
          </form>
        </div>
      </div>

      {isConfirmingPostDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
          <div className="flex w-full max-w-100 flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg">
            <p className="text-lg font-semibold text-text">이 글을 삭제할까요?</p>
            <p className="text-[13px] leading-relaxed text-text-muted">
              삭제하면 글과 모든 댓글이 영구히 사라져요. 되돌릴 수 없어요.
            </p>
            {deletePostMutation.isError && (
              <p className="text-xs text-danger">삭제에 실패했어요, 다시 시도해주세요</p>
            )}
            <footer className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setIsConfirmingPostDelete(false)}
                disabled={deletePostMutation.isPending}
                className="rounded-md px-4 py-2.5 text-[13px] font-medium text-text-muted"
              >
                취소
              </button>
              <button
                type="button"
                onClick={() => deletePostMutation.mutate()}
                disabled={deletePostMutation.isPending}
                className="rounded-md bg-danger px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
              >
                {deletePostMutation.isPending ? '삭제 중...' : '삭제'}
              </button>
            </footer>
          </div>
        </div>
      )}

      {commentDeleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
          <div className="flex w-full max-w-100 flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg">
            <p className="text-lg font-semibold text-text">이 댓글을 삭제할까요?</p>
            <p className="text-[13px] leading-relaxed text-text-muted">되돌릴 수 없어요.</p>
            {deleteCommentMutation.isError && (
              <p className="text-xs text-danger">삭제에 실패했어요, 다시 시도해주세요</p>
            )}
            <footer className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setCommentDeleteTarget(null)}
                disabled={deleteCommentMutation.isPending}
                className="rounded-md px-4 py-2.5 text-[13px] font-medium text-text-muted"
              >
                취소
              </button>
              <button
                type="button"
                onClick={() => deleteCommentMutation.mutate(commentDeleteTarget)}
                disabled={deleteCommentMutation.isPending}
                className="rounded-md bg-danger px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
              >
                {deleteCommentMutation.isPending ? '삭제 중...' : '삭제'}
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  )
}
