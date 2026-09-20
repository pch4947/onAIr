import { WebSocketServer } from 'ws'

const PORT = 8787
const FORCE_DISCONNECT_MS = 15000

const wss = new WebSocketServer({ port: PORT })

console.log(`[mock-ws] listening on ws://localhost:${PORT}`)
console.log(`[mock-ws] connected clients get force-disconnected after ${FORCE_DISCONNECT_MS / 1000}s to exercise reconnect logic`)

wss.on('connection', (socket) => {
  console.log('[mock-ws] client connected')

  socket.on('message', (data) => {
    try {
      const message = JSON.parse(data.toString())
      console.log('[mock-ws] received', message)
      socket.send(JSON.stringify({ type: 'echo', payload: message }))
    } catch {
      console.log('[mock-ws] received non-JSON message, ignoring:', data.toString())
    }
  })

  const disconnectTimer = setTimeout(() => {
    console.log('[mock-ws] forcing disconnect to test client reconnect')
    socket.close()
  }, FORCE_DISCONNECT_MS)

  socket.on('close', () => {
    clearTimeout(disconnectTimer)
    console.log('[mock-ws] client disconnected')
  })
})
