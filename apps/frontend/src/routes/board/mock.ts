// FEATURE: 04_Board_게시판 목업 데이터 (추후 REST 응답으로 교체 예정)

export interface BoardPost {
  id: string
  author: string
  content: string
  status: string
  hasReplay: boolean
}

export const BOARD_POSTS: BoardPost[] = [
  {
    id: '1',
    author: '익명',
    content: '오늘 하루 정말 힘들었는데 위로되는 곡 하나 부탁드려요',
    status: 'GENERATING',
    hasReplay: false,
  },
  {
    id: '2',
    author: '라디오러',
    content: '무드: 잔잔한 밤 — 잔잔한 곡으로 부탁드려요',
    status: 'QUEUED',
    hasReplay: false,
  },
  {
    id: '3',
    author: '별밤지기',
    content: '친구랑 다퉜는데 화해하고 싶어요. 응원 한마디 부탁드려요',
    status: '방송에 반영됨 · PLAYED',
    hasReplay: true,
  },
  {
    id: '4',
    author: '익명',
    content: '신나는 곡으로 부탁드려요!',
    status: '대기 중',
    hasReplay: false,
  },
  {
    id: '5',
    author: '조용한밤',
    content: '오늘 면접 합격 후기 나누고 싶어요 :)',
    status: '방송에 반영됨 · PLAYED',
    hasReplay: true,
  },
]
