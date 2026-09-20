export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected'

type MessageHandler<T> = (payload: T) => void
type StatusHandler = (status: ConnectionStatus) => void

const INITIAL_BACKOFF_MS = 500
const MAX_BACKOFF_MS = 15000

export class RealtimeClient {
  private url: string
  private socket: WebSocket | null
  private handlers: Map<string, Set<MessageHandler<unknown>>>
  private statusHandlers: Set<StatusHandler>
  private backoffMs: number
  private reconnectTimer: ReturnType<typeof setTimeout> | null
  private closedByUser: boolean

  constructor(url: string) {
    this.url = url
    this.socket = null
    this.handlers = new Map()
    this.statusHandlers = new Set()
    this.backoffMs = INITIAL_BACKOFF_MS
    this.reconnectTimer = null
    this.closedByUser = false
  }

  connect(): void {
    if (!this.url) {
      console.warn('[RealtimeClient] VITE_WS_URL이 비어있어 연결을 시도하지 않습니다.')
      return
    }

    this.closedByUser = false
    this.setStatus('connecting')
    this.socket = new WebSocket(this.url)

    this.socket.addEventListener('open', () => {
      this.backoffMs = INITIAL_BACKOFF_MS
      this.setStatus('connected')
    })

    this.socket.addEventListener('message', (event) => {
      this.dispatch(event.data)
    })

    this.socket.addEventListener('close', () => {
      this.setStatus('disconnected')
      if (!this.closedByUser) {
        this.scheduleReconnect()
      }
    })

    this.socket.addEventListener('error', () => {
      this.socket?.close()
    })
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
    try {
      const message = JSON.parse(raw) as { type: string; payload: unknown }
      const handlers = this.handlers.get(message.type)
      handlers?.forEach((handler) => handler(message.payload))
    } catch {
      // 파싱 불가능한 메시지는 무시
    }
  }

  private setStatus(status: ConnectionStatus): void {
    this.statusHandlers.forEach((handler) => handler(status))
  }

  on<T>(type: string, handler: MessageHandler<T>): () => void {
    if (!this.handlers.has(type)) {
      this.handlers.set(type, new Set())
    }
    this.handlers.get(type)?.add(handler as MessageHandler<unknown>)
    return () => this.handlers.get(type)?.delete(handler as MessageHandler<unknown>)
  }

  onStatusChange(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler)
    return () => this.statusHandlers.delete(handler)
  }

  send<T>(type: string, payload: T): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return
    this.socket.send(JSON.stringify({ type, payload }))
  }

  close(): void {
    this.closedByUser = true
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.socket?.close()
    this.setStatus('disconnected')
  }
}
