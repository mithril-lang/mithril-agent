import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import { createInterface } from 'node:readline'
import { readFileSync, writeFileSync } from 'node:fs'
const input = createInterface({ input: process.stdin })
const config = await new Promise(resolve => input.once('line', line => resolve(JSON.parse(line))))
input.close()
process.stdin.destroy()
const sdk = await import(pathToFileURL(process.argv[2]).href)
const callOwnedTool = process.argv[3]
  ? (await import(pathToFileURL(process.argv[3]).href)).callDashboardOwnedTool
  : sdk.callOwnedTool
async function connect(ticket) {
  const socket = new WebSocket(config.url, ticket ? ['hermes-gateway-v1', `hermes-gateway-ticket.${ticket}`] : [])
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('upgrade deadline')), 10000)
    socket.addEventListener(
      'open',
      () => {
        clearTimeout(timer)
        resolve()
      },
      { once: true }
    )
    socket.addEventListener(
      'error',
      () => {
        clearTimeout(timer)
        reject(Error('upgrade refused'))
      },
      { once: true }
    )
  })
  assert.equal(socket.protocol, 'hermes-gateway-v1')
  let id = 0
  const pending = new Map()
  const client = {
    connected: true,
    connectionEpoch: 1,
    request(method, params = {}, timeout = 25000) {
      const requestId = ++id
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(requestId)
          reject(Error('RPC deadline'))
        }, timeout)
        pending.set(requestId, { resolve, reject, timer })
        socket.send(JSON.stringify({ jsonrpc: '2.0', id: requestId, method, params }))
      })
    }
  }
  socket.addEventListener('message', event => {
    const frame = JSON.parse(event.data),
      waiting = pending.get(frame.id)
    if (!waiting) return
    pending.delete(frame.id)
    clearTimeout(waiting.timer)
    if (frame.error) waiting.reject(Error('RPC refused'))
    else waiting.resolve(frame.result)
  })
  socket.addEventListener('close', () => {
    client.connected = false
    client.connectionEpoch++
    for (const waiting of pending.values()) {
      clearTimeout(waiting.timer)
      waiting.reject(Error('connection closed'))
    }
    pending.clear()
  })
  return {
    client,
    close: async () => {
      if (!client.connected) return
      await new Promise(resolve => {
        socket.addEventListener('close', resolve, { once: true })
        socket.close()
      })
    }
  }
}
await assert.rejects(connect(null))
const first = await connect(config.tickets[0])
let second
try {
  await assert.rejects(first.client.request('tools.show', { session_id: 'a' }))
  const resumed = await first.client.request('session.resume', {
    session_id: 'same-durable-owner',
    profile: 'a',
    lazy: true
  })
  assert.equal(resumed.session_id, 'a')
  const invoke = (client, name, args, requestId) =>
    callOwnedTool(client, {
      sessionId: 'a',
      name,
      arguments: args,
      requestId,
      timeoutMs: 20000,
      current: () => client.connected
    })
  const read = await invoke(first.client, 'read_file', { path: config.input }, 'ws-read')
  assert.ok(read.output.content.includes('ws-owned'))
  const args = { path: config.output, content: 'ws-effect' }
  const written = await invoke(first.client, 'write_file', args, 'ws-write')
  assert.equal(written.state, 'returned')
  assert.equal(readFileSync(config.output, 'utf8'), 'ws-effect')
  writeFileSync(config.output, 'effect-already-delivered')
  await first.close()
  await assert.rejects(connect(config.tickets[0]))
  second = await connect(config.tickets[1])
  const reattached = await second.client.request('session.resume', {
    session_id: 'same-durable-owner',
    profile: 'a',
    lazy: true
  })
  assert.equal(reattached.session_id, 'a')
  const replay = await invoke(second.client, 'write_file', args, 'ws-write')
  assert.equal(replay.observation, 'metadata-only')
  assert.equal(replay.output, null)
  assert.equal(readFileSync(config.output, 'utf8'), 'effect-already-delivered')
  const page = await second.client.request('tools.attempts', { session_id: 'a' })
  assert.ok(page.attempts.some(row => row.attempt_id === 'rpc:ws-write' && row.state === 'returned'))
  process.stdout.write(JSON.stringify({ passed: true, transport: 'authenticated-websocket', reconnect: true }) + '\n')
} finally {
  await first.close()
  if (second) await second.close()
}
