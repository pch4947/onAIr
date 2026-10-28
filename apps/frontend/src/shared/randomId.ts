// crypto.randomUUID()는 보안 컨텍스트(HTTPS/localhost)에서만 있어서, http LAN 접속용 대체값을 둔다
export function randomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`
}
