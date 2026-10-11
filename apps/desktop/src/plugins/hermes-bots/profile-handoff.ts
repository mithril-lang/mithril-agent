/** Single-writer transfer orchestration. No archive, key or release proof is persisted. */
export interface HandoffCapsule {
  profile_name: string
  identity: string
  generation: number
  operation: string
  target: string
  sha256: string
  release_hash: string
  archive: string
}

export interface HandoffState {
  phase?: 'unmanaged' | 'active' | 'frozen' | 'moved' | 'staged' | null
  operation?: string | null
  target?: string | null
  sha256?: string | null
  gateway?: string | null
  proof?: string | null
  capsule?: HandoffCapsule | null
}

export type HandoffCall = (params: Record<string, unknown>) => Promise<HandoffState>

export async function moveTrialProfile(source: HandoffCall, destination: HandoffCall): Promise<void> {
  const [sourceGateway, targetGateway, sourceState] = await Promise.all([
    source({ action: 'gateway' }),
    destination({ action: 'gateway' }),
    source({ action: 'status' })
  ])

  if (!targetGateway.gateway || sourceGateway.gateway === targetGateway.gateway) {
    throw new Error('Choose a different gateway for this profile')
  }

  let state = sourceState

  if (state.phase === 'unmanaged') {
    state = await source({ action: 'enroll' })
  }

  const operation = state.phase === 'active' ? crypto.randomUUID().replaceAll('-', '') : state.operation

  if (!operation || (state.phase !== 'active' && state.target !== targetGateway.gateway)) {
    throw new Error('Resume the pending move to its original destination')
  }

  let digest = state.sha256

  if (state.phase !== 'moved') {
    await source({ action: 'freeze', operation, target: targetGateway.gateway })

    const key = Array.from(crypto.getRandomValues(new Uint8Array(32)), byte => byte.toString(16).padStart(2, '0')).join(
      ''
    )

    const exported = await source({ action: 'export', operation, encryption_key: key })

    if (!exported.capsule) {
      throw new Error('The gateway did not return a verified transfer')
    }

    digest = exported.capsule.sha256
    await destination({ action: 'stage', capsule: exported.capsule, encryption_key: key })
  } else {
    const staged = await destination({ action: 'status' })

    if (
      staged.operation !== operation ||
      staged.sha256 !== digest ||
      !['staged', 'active'].includes(staged.phase || '')
    ) {
      throw new Error('Destination ownership does not match the released transfer')
    }
  }

  const released = await source({ action: 'release', operation, sha256: digest })

  if (!released.proof) {
    throw new Error('The source did not confirm release; both copies remain fenced')
  }

  await destination({ action: 'activate', operation, proof: released.proof })
  const [oldOwner, newOwner] = await Promise.all([source({ action: 'status' }), destination({ action: 'status' })])

  if (oldOwner.phase !== 'moved' || newOwner.phase !== 'active' || newOwner.operation !== operation) {
    throw new Error('Ownership read-back failed; inspect the pending move before continuing')
  }
}
