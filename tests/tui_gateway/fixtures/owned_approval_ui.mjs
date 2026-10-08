import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { readFileSync } from 'node:fs'

const config = JSON.parse(readFileSync(0, 'utf8'))
const require = createRequire(pathToFileURL(`${config.dependencies}/../package.json`))
const { build } = require('esbuild')
const { JSDOM } = require('jsdom')
const manifest = JSON.parse(readFileSync(`${config.sdk}/package.json`, 'utf8'))
const sdkAliases = Object.fromEntries(['approval-card-react', 'chat-approval', 'owned-gateway-approvals', 'owned-gateway-tools'].map(name => {
  const entry = manifest.exports[`./${name}`]
  return [`@mithril/workspace/${name}`, `${config.sdk}/${typeof entry === 'string' ? entry : entry.default}`]
}))
const NativeWebSocket = globalThis.WebSocket
const dom = new JSDOM('<div id="root"></div>', { url: config.http })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const wait = async predicate => {
  const deadline = Date.now() + 10000
  while (!predicate()) {
    if (Date.now() >= deadline) throw Error('qualification deadline')
    await new Promise(resolve => setTimeout(resolve, 10))
  }
}
const control = async method => {
  const response = await fetch(`${config.http}/qualification/${method}`, { method: 'POST' })
  assert.equal(response.status, 200)
  return response.json()
}
const resolvedOnce = async () => {
  const deadline = Date.now() + 10000
  let state
  do {
    state = await control('state')
    if (state.choice === 'once') break
    assert.ok(Date.now() < deadline, 'real queue resolution deadline')
  } while (true)
  assert.equal(state.pending, false)
}
let ticketIndex = 0
globalThis.WebSocket = class extends NativeWebSocket {
  constructor(url) {
    super(url, ['hermes-gateway-v1', `hermes-gateway-ticket.${config.tickets[ticketIndex++]}`])
  }
}
const reports = []
for (const [surface, source] of Object.entries(config.clients)) {
  const outfile = `${config.output}/${surface}.mjs`
  await build({
    stdin: { contents: `export { DashboardGatewayClient } from ${JSON.stringify(source)};
export { SharedApprovalCard } from '@mithril/workspace/approval-card-react';
export { normalizeApprovalRequest } from '@mithril/workspace/chat-approval';
export { createRoot } from 'react-dom/client';
export { createElement, act } from 'react';`, resolveDir: config.dependencies },
    bundle: true, format: 'esm', platform: 'browser', outfile,
    nodePaths: [config.dependencies],
    alias: { ...sdkAliases, react: `${config.dependencies}/react`, 'react-dom': `${config.dependencies}/react-dom` }
  })
  const { DashboardGatewayClient, SharedApprovalCard, normalizeApprovalRequest, createRoot, createElement, act } =
    await import(pathToFileURL(outfile).href)
  const events = []
  process.stderr.write(`qualification ${surface}: connect\n`)
  const client = new DashboardGatewayClient({ acceptApprovalRequests: true, onEvent: event => events.push(event) })
  await client.connect(config.ws)
  await client.request('client.capabilities', { server_requests: true })
  await client.request('session.resume', { session_id: 'same-durable-owner', profile: 'a', lazy: true })
  for (const mode of ['once', 'cancel', 'disconnect']) {
    process.stderr.write(`qualification ${surface}: ${mode}\n`)
    const start = events.length
    await control('begin')
    await wait(() => events.slice(start).some(event => event.type === 'approval.request'))
    let peer = events.slice(start).find(event => event.type === 'approval.request')
    assert.equal(peer.session_id, 'a')
    assert.equal(client.answerApproval(peer.payload.server_request_id, 'b', 'once'), false)
    let msg = normalizeApprovalRequest(peer.payload, peer.payload.request_id)
    assert.deepEqual(msg.choices, ['once', 'deny'])
    let queued = false
    const root = createRoot(document.getElementById('root'))
    const render = unavailable => root.render(createElement(SharedApprovalCard, {
      msg: { ...msg, unavailable }, translate: key => key,
      onRespond: async (_, choice) => client.answerApproval(peer.payload.server_request_id, 'a', choice),
      onResolved: () => { queued = true }
    }))
    try {
      await act(async () => render(false))
      const button = [...document.querySelectorAll('button')].find(item => item.textContent === 'chat.approval.once')
      assert.ok(button)
      assert.equal((await control('state')).pending, true)
      if (mode === 'once') {
        await act(async () => button.click())
        await wait(() => queued)
        await resolvedOnce()
        assert.equal(client.answerApproval(peer.payload.server_request_id, 'a', 'once'), false)
      } else if (mode === 'cancel') {
        await control('cancel')
        await wait(() => events.slice(start).some(event => event.type === 'approval.cancel'))
        await act(async () => render(true))
        assert.equal(document.querySelectorAll('button').length, 0)
        assert.equal(client.answerApproval(peer.payload.server_request_id, 'a', 'once'), false)
        assert.equal((await control('state')).choice, null)
      } else {
        client.close()
        await act(async () => render(true))
        assert.equal(document.querySelectorAll('button').length, 0)
        assert.equal(client.answerApproval(peer.payload.server_request_id, 'a', 'once'), false)
        const reconnectStart = events.length
        await client.connect(config.ws)
        await client.request('client.capabilities', { server_requests: true })
        const resumed = await client.request('session.resume', { session_id: 'same-durable-owner', profile: 'a', lazy: true })
        assert.ok(resumed.open_requests.some(item => item.id === peer.payload.server_request_id))
        assert.equal((await control('state')).pending, true)
        const restored = events.slice(reconnectStart).find(event => event.type === 'approval.request')
        assert.ok(restored, 'actual attachment snapshot must restore the original peer request')
        assert.equal(restored.payload.server_request_id, peer.payload.server_request_id)
        peer = restored
        msg = normalizeApprovalRequest(peer.payload, peer.payload.request_id)
        await act(async () => render(false))
        const restoredButton = [...document.querySelectorAll('button')].find(item => item.textContent === 'chat.approval.once')
        await act(async () => restoredButton.click())
        await wait(() => queued)
        await resolvedOnce()
        assert.equal(client.answerApproval(peer.payload.server_request_id, 'a', 'once'), false)
      }
      reports.push({ surface, mode, passed: true })
    } finally {
      await act(async () => root.unmount())
    }
  }
  client.close()
}
dom.window.close()
// Browser-bundled React's scheduler keeps Node MessagePorts alive after unmount.
// All asserted work is complete and both clients were explicitly closed.
process.stdout.write(JSON.stringify(reports) + '\n', () => process.exit(0))
