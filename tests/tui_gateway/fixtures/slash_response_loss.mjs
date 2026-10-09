import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import { createInterface } from 'node:readline'

const input = createInterface({ input: process.stdin })
const config = await new Promise(resolve => input.once('line', line => resolve(JSON.parse(line))))
input.close()
process.stdin.destroy()
const { DashboardGatewayClient, executeSlash } = await import(pathToFileURL(process.argv[2]).href)
const NativeWebSocket = globalThis.WebSocket
let ticket
// Node timer and upgrade adapters only; request/response routing is the actual client.
globalThis.window = { setTimeout, clearTimeout }
globalThis.WebSocket = class extends NativeWebSocket {
  constructor(url) {
    super(url, ['hermes-gateway-v1', `hermes-gateway-ticket.${ticket}`])
  }
}
const client = new DashboardGatewayClient({ requestTimeoutMs: 15000 })
let ticketIndex = 0
const sent = []
const request = (method, params) => {
  sent.push(method)
  return client.request(method, params)
}
async function attach(owner) {
  ticket = config.tickets[ticketIndex++]
  await client.connect(config.url)
  const result = await request('session.resume', {
    session_id: 'same-durable-owner',
    profile: owner,
    lazy: true
  })
  assert.equal(result.session_id, owner)
}
try {
  for (const visit of config.visits) {
    await attach(visit.owner)
    const review = await request('slash.exec', {
      session_id: visit.owner,
      command: `memory review ${visit.id}`
    })
    const detail = JSON.parse(review.output)
    assert.equal(detail.pending_id, visit.id)
    const command = `memory approve ${visit.id} ${detail.review_digest}`
    if (config.client === 'desktop') {
      const outcome = await executeSlash({
        command: `/${command}`,
        sessionId: visit.owner,
        request,
        sys: () => assert.fail('lost result must not render a successful approval')
      })
      assert.equal(outcome.kind, 'error')
    } else {
      await assert.rejects(request('slash.exec', { session_id: visit.owner, command }))
    }
    assert.equal(sent.filter(method => method === 'command.dispatch').length, 0)
    await attach(visit.owner)
    const queue = JSON.parse(
      (
        await request('slash.exec', {
          session_id: visit.owner,
          command: 'memory review'
        })
      ).output
    )
    assert.ok(!queue.pending.some(row => row.pending_id === visit.id))
    client.close()
  }
  console.log(JSON.stringify({ passed: true, visits: config.visits.length, redispatch: 0 }))
} finally {
  client.close()
}
