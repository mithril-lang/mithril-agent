// Cross-language wire qualifier. stdout contains only JSON-RPC; no model/provider calls.
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import { createInterface } from 'node:readline'
const sdk = await import(pathToFileURL(process.argv[2]).href)
const callOwnedTool = process.argv[5]
  ? (await import(pathToFileURL(process.argv[5]).href)).callDashboardOwnedTool
  : sdk.callOwnedTool
const mode = process.argv[3]
const roots = JSON.parse(process.argv[4])
let serial = 0
const pending = new Map()
const reader = createInterface({ input: process.stdin })
reader.on('line', line => {
  const frame = JSON.parse(line)
  const waiting = pending.get(frame.id)
  if (!waiting) return
  pending.delete(frame.id)
  clearTimeout(waiting.timer)
  if (frame.error) waiting.reject(Error('RPC rejected'))
  else waiting.resolve(frame.result)
})
const client = {
  connected: true,
  connectionEpoch: 1,
  request(method, params = {}, timeout = 25000) {
    const id = ++serial
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id)
        reject(Error('RPC deadline'))
      }, timeout)
      pending.set(id, { resolve, reject, timer })
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n')
    })
  }
}
let owner = 'a'
const invoke = (name, args, requestId, target = owner) =>
  callOwnedTool(client, {
    sessionId: target,
    requestId,
    name,
    arguments: args,
    timeoutMs: 20000,
    current: () => client.connected
  })
try {
  if (mode === 'roundtrip') {
    for (const [visit, profile] of ['a', 'b', 'a'].entries()) {
      owner = profile
      await client.request('fixture.select', { owner })
      client.connectionEpoch++
      const read = await invoke('read_file', { path: `${roots[owner]}/owned-${visit}.txt` }, `read-${visit}`)
      assert.equal(read.observation, 'handler-return')
      assert.ok(read.output.content.includes(`${owner}-owned`))
      for (const target of ['content', 'files']) {
        for (const outputMode of ['content', 'files_only', 'count']) {
          const args = {
            pattern: target === 'content' ? `${owner}-owned` : `owned-${visit}.txt`,
            path: roots[owner], target, output_mode: outputMode,
            file_glob: `owned-${visit}.txt`
          }
          const requestId = `search-${visit}-${target}-${outputMode}`
          const search = await invoke('search_files', args, requestId)
          assert.equal(search.state, 'returned')
          const output = JSON.stringify(search.output)
          assert.ok(output.includes(`owned-${visit}.txt`), output)
          assert.ok(output.includes(roots[owner]), output)
          assert.ok(!output.includes(roots[owner === 'a' ? 'b' : 'a']), output)
          const replay = await invoke('search_files', args, requestId)
          assert.equal(replay.observation, 'metadata-only')
          assert.equal(replay.output, null)
        }
      }
      const patchArgs = { mode: 'patch', patch: [
        '*** Begin Patch', `*** Update File: ${roots[owner]}/patch-update-${visit}.txt`, '@@', '-before', '+after',
        `*** Add File: ${roots[owner]}/patch-added-${visit}.txt`, '+added',
        `*** Delete File: ${roots[owner]}/patch-delete-${visit}.txt`,
        `*** Move File: ${roots[owner]}/patch-move-from-${visit}.txt -> ${roots[owner]}/patch-move-to-${visit}.txt`,
        '*** End Patch'
      ].join('\n') };
      const patch = await invoke('patch', patchArgs, `patch-${visit}`);
      assert.equal(patch.state, 'returned');
      assert.ok(!patch.output.error, JSON.stringify(patch.output));
      const patchReplay = await invoke('patch', patchArgs, `patch-${visit}`);
      assert.equal(patchReplay.observation, 'metadata-only');
      assert.equal(patchReplay.output, null);
      await assert.rejects(
        invoke('read_file', { path: `${roots.b}/owned.txt` }, `foreign-${visit}`, owner === 'a' ? 'b' : 'a'),
        e => e.outcome === 'not-dispatched'
      )
      const path = `${roots[owner]}/sdk-${visit}.txt`
      const args = { path, content: `${owner}-effect-${visit}` }
      const write = await invoke('write_file', args, `write-${visit}`)
      assert.equal(write.state, 'returned')
      await client.request('fixture.mark', { path })
      const replay = await invoke('write_file', args, `write-${visit}`)
      assert.equal(replay.observation, 'metadata-only')
      assert.equal(replay.duplicate, true)
      assert.equal(replay.output, null)
      const todo = await invoke(
        'todo_list',
        { todos: [{ id: 'sdk', content: `${owner}-todo`, status: 'pending' }] },
        `todo-${visit}`
      )
      assert.equal(todo.observation, 'handler-return')
      const prior = await invoke(
        'execute_code',
        { code: "print(globals().get('sdk_profile_value', 'no-prior-value'))" },
        `prior-${visit}`
      )
      assert.equal(prior.output.output.trim(), visit === 2 ? 'a' : 'no-prior-value')
      const codeArgs = {
        code: `sdk_profile_value = ${JSON.stringify(owner)}\nfrom hermes_tools import read_file\nprint(read_file(${JSON.stringify(`${roots[owner]}/child-${visit}.txt`)}))`
      }
      const code = await invoke('execute_code', codeArgs, `python-${visit}`)
      assert.equal(code.observation, 'handler-return')
      assert.ok(code.output.output.includes(`${owner}-owned`), JSON.stringify(code.output))
      const codeReplay = await invoke('execute_code', codeArgs, `python-${visit}`)
      assert.equal(codeReplay.observation, 'metadata-only')
      assert.equal(codeReplay.output, null)
      const peek = await invoke('execute_code', { code: 'print(sdk_profile_value)' }, `peek-${visit}`)
      assert.equal(peek.output.output.trim(), owner)
      const metadata = await client.request('tools.attempts', { session_id: owner })
      assert.ok(metadata.attempts.some(row => row.attempt_id === write.attemptId))
    }
  } else {
    const path = `${roots.a}/lost.txt`,
      args = { path, content: 'effect-before-loss' }
    const lost = await invoke('write_file', args, 'lost-stable')
    assert.equal(lost.state, 'running')
    assert.equal(lost.terminal, false)
    assert.equal(lost.observation, 'unknown')
    assert.equal(lost.output, null)
    await client.request('fixture.mark', { path })
    const replay = await invoke('write_file', args, 'lost-stable')
    assert.equal(replay.duplicate, true)
    assert.equal(replay.observation, 'metadata-only')
    assert.equal(replay.state, 'running')
    assert.equal(replay.output, null)
    const page = await client.request('tools.attempts', { session_id: 'a' })
    assert.ok(page.attempts.some(row => row.attempt_id === 'rpc:lost-stable' && row.state === 'running'))
  }
  await client.request('fixture.report', { mode, passed: true })
} finally {
  client.connected = false
  for (const { timer } of pending.values()) clearTimeout(timer)
  reader.close()
  process.stdin.destroy()
}
