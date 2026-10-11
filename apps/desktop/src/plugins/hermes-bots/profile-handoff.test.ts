import { describe, expect, it, vi } from 'vitest'

import { type HandoffCall, type HandoffCapsule, type HandoffState, moveTrialProfile } from './profile-handoff'

function pair() {
  const events: string[] = []
  let source: HandoffState = { phase: 'active' }
  let destination: HandoffState = { phase: 'unmanaged' }

  const capsule: HandoffCapsule = {
    profile_name: 'handoff-trial-profile',
    identity: 'same-profile',
    generation: 1,
    operation: '',
    target: 'spaces',
    sha256: 'latest-history',
    release_hash: 'release-hash',
    archive: 'opaque-encrypted-profile'
  }

  const sourceCall: HandoffCall = vi.fn(async params => {
    events.push(`source:${params.action}`)

    switch (params.action) {
      case 'gateway':
        return { gateway: 'local' }

      case 'status':
        return { ...source }

      case 'freeze':
        source = { phase: 'frozen', operation: String(params.operation), target: 'spaces' }
        capsule.operation = String(params.operation)

        return { ...source }

      case 'export':
        source.sha256 = capsule.sha256

        return { capsule: { ...capsule } }

      case 'release':
        source.phase = 'moved'

        return { proof: 'released-only-after-source-is-fenced' }

      default:
        throw new Error('Unexpected source action')
    }
  })

  const destinationCall: HandoffCall = vi.fn(async params => {
    events.push(`destination:${params.action}`)

    switch (params.action) {
      case 'gateway':
        return { gateway: 'spaces' }

      case 'status':
        return { ...destination }

      case 'stage':
        destination = { phase: 'staged', operation: capsule.operation, sha256: capsule.sha256 }

        return { ...destination }

      case 'activate':
        expect(source.phase).toBe('moved')
        expect(params.proof).toBe('released-only-after-source-is-fenced')
        destination.phase = 'active'

        return { ...destination }

      default:
        throw new Error('Unexpected destination action')
    }
  })

  return { events, sourceCall, destinationCall, source: () => source, destination: () => destination }
}

describe('Desktop profile handoff', () => {
  it('stages the latest history before release and activates only after release', async () => {
    const trial = pair()
    await moveTrialProfile(trial.sourceCall, trial.destinationCall)
    expect(trial.events.indexOf('destination:stage')).toBeLessThan(trial.events.indexOf('source:release'))
    expect(trial.events.indexOf('source:release')).toBeLessThan(trial.events.indexOf('destination:activate'))
    expect(trial.source().phase).toBe('moved')
    expect(trial.destination().phase).toBe('active')
  })

  it('never releases the source or activates the destination after failed staging', async () => {
    const trial = pair()

    const destination: HandoffCall = params =>
      params.action === 'stage' ? Promise.reject(new Error('Network unavailable')) : trial.destinationCall(params)

    await expect(moveTrialProfile(trial.sourceCall, destination)).rejects.toThrow('Network unavailable')
    expect(trial.source().phase).toBe('frozen')
    expect(trial.events).not.toContain('source:release')
    expect(trial.events).not.toContain('destination:activate')
  })

  it('recovers a lost release response without reactivating the source or retransferring stale history', async () => {
    const trial = pair()
    let loseResponse = true

    const source: HandoffCall = async params => {
      const result = await trial.sourceCall(params)

      if (params.action === 'release' && loseResponse) {
        loseResponse = false
        throw new Error('Release response lost')
      }

      return result
    }

    await expect(moveTrialProfile(source, trial.destinationCall)).rejects.toThrow('Release response lost')
    expect(trial.source().phase).toBe('moved')
    expect(trial.destination().phase).toBe('staged')
    await moveTrialProfile(source, trial.destinationCall)
    expect(trial.destination().phase).toBe('active')
    expect(trial.events.filter(event => event === 'source:export')).toHaveLength(1)
  })
})
