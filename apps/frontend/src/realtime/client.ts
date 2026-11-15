export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected'

// 서버가 보내는 메시지 (#312). 필드가 메시지 최상위에 있다
export interface RealtimeMessage {
  type: string
  [key: string]: unknown
}

type MessageHandler = (message: RealtimeMessage) => void
type StatusHandler = (status: ConnectionStatus) => void

const INITIAL_BACKOFF_MS = 500
const MAX_BACKOFF_MS = 15000
// 서버는 20초마다 ping을 보낸다. 이만큼 아무것도 안 오면 끊긴 것으로 보고 다시 붙는다 (모바일 절전·네트워크 전환)
const SILENCE_LIMIT_MS = 45000
// 화면으로 돌아왔을 때 이만큼 조용했으면 소켓이 죽었을 수 있어 바로 다시 붙는다 (모바일 백그라운드)
const STALE_ON_RETURN_MS = 25000
// 서버가 이 코드로 닫으면(로그인 필요·없는 방송) 다시 붙어도 같으므로 멈춘다. 폴링은 원래 주기로 돈다
const FINAL_CLOSE_CODES = new Set([4401, 4404])

export class RealtimeClient {
  private url: string
  private getToken: () => string | null
  private socket: WebSocket | null = null
  private handlers = new Map<string, Set<MessageHandler>>()
  private statusHandlers = new Set<StatusHandler>()
  private backoffMs = INITIAL_BACKOFF_MS
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private silenceTimer: ReturnType<typeof setTimeout> | null = null
  private closedByUser = false
  private lastMessageAt = 0

  constructor(url: string, getToken: () => string | null) {
    this.url = url
    this.getToken = getToken
    // 서버가 토큰을 받아들여 hello를 보내야 연결된 것이다 — 거절(4401)이면 연결됨이 잠깐도 뜨지 않게
    this.on('hello', () => {
      this.backoffMs = INITIAL_BACKOFF_MS
      this.setStatus('connected')
    })
  }

  connect(): void {
    this.closedByUser = false
    this.setStatus('connecting')
    const socket = new WebSocket(this.url)
    this.socket = socket

    socket.addEventListener('open', () => {
      // 토큰은 주소에 넣으면 서버 접근 로그에 남아서 첫 메시지로 보낸다. 내 채팅 여부(mine)를 서버가 정하는 데 쓴다
      socket.send(JSON.stringify({ type: 'auth', token: this.getToken() }))
      this.watchSilence()
    })

    socket.addEventListener('message', (event) => {
      this.lastMessageAt = Date.now()
      this.watchSilence()
      this.dispatch(event.data)
    })

    socket.addEventListener('close', (event) => {
      if (this.socket !== socket) return
      this.socket = null
      this.clearSilence()
      this.setStatus('disconnected')
      if (!this.closedByUser && !FINAL_CLOSE_CODES.has(event.code)) this.scheduleReconnect()
    })

    socket.addEventListener('error', () => socket.close())
  }

  // 화면 복귀·네트워크 복구 때 부른다. 끊겨서 다시 붙기를 기다리는 중이면 바로 붙고, 조용히 죽은 소켓이면 끊고 다시 붙는다
  wake(): void {
    if (this.closedByUser) return
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
      this.backoffMs = INITIAL_BACKOFF_MS
      this.connect()
    } else if (this.socket?.readyState === WebSocket.OPEN && Date.now() - this.lastMessageAt > STALE_ON_RETURN_MS) {
      this.socket.close()
    }
  }

  private watchSilence(): void {
    this.clearSilence()
    this.silenceTimer = setTimeout(() => this.socket?.close(), SILENCE_LIMIT_MS)
  }

  private clearSilence(): void {
    if (this.silenceTimer) clearTimeout(this.silenceTimer)
    this.silenceTimer = null
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.backoffMs = Math.min(this.backoffMs * 2, MAX_BACKOFF_MS)
      this.connect()
    }, this.backoffMs)
  }

  private dispatch(raw: string): void {
    let message: RealtimeMessage
    try {
      message = JSON.parse(raw) as RealtimeMessage
    } catch {
      return // 파싱 불가능한 메시지는 무시
    }
    this.handlers.get(message.type)?.forEach((handler) => handler(message))
  }

  private setStatus(status: ConnectionStatus): void {
    this.statusHandlers.forEach((handler) => handler(status))
  }

  on(type: string, handler: MessageHandler): () => void {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set())
    this.handlers.get(type)?.add(handler)
    return () => this.handlers.get(type)?.delete(handler)
  }

  onStatusChange(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler)
    return () => this.statusHandlers.delete(handler)
  }

  close(): void {
    this.closedByUser = true
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    this.clearSilence()
    const socket = this.socket
    this.socket = null
    socket?.close()
    this.setStatus('disconnected')
  }
}
