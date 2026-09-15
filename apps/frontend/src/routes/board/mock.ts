// FEATURE: 04_Board_게시판 목업 데이터 (추후 REST 응답으로 교체 예정)

export interface BoardPost {
  id: string
  title: string
  author: string
  timeAgo: string
}

export const BOARD_POSTS: BoardPost[] = [
  { id: '1', title: '오늘 하루 위로가 필요해요', author: '익명', timeAgo: '2분 전' },
  { id: '2', title: '잔잔한 밤, 듣고 싶은 곡이 있어요', author: '라디오러버', timeAgo: '5분 전' },
  { id: '3', title: '친구와 화해하고 싶어요', author: '별밤지기', timeAgo: '12분 전' },
  { id: '4', title: '신나는 곡 추천해주세요', author: '익명', timeAgo: '20분 전' },
  { id: '5', title: '면접 합격 후기 나누고 싶어요', author: '조용한밤', timeAgo: '34분 전' },
]
