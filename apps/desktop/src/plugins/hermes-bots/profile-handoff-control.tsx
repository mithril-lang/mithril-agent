import {
  Button,
  host,
  queryClient,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import { saveSelectedRosterBot } from './bot-state'
import { openBotCanonicalChat } from './canonical-chat'
import { ROSTER_KEY } from './data'
import { useBots } from './i18n'
import { type HandoffCall, type HandoffState, moveTrialProfile } from './profile-handoff'
import { botConnectionRoute } from './routing'
import type { ConnectionRow, RosterRow } from './types'

/** Deliberately limited to new API trial profiles; existing bot controls stay untouched. */
export function ProfileHandoffControl({ bot, onComplete }: { bot: RosterRow; onComplete: () => void }) {
  const b = useBots().handoff
  const [connections, setConnections] = useState<ConnectionRow[]>([])
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const owner = botConnectionRoute(bot)
  const sourceId = owner?.connectionId || String(host.state?.connectionId?.get?.() || 'local')

  useEffect(() => {
    if (typeof host.connections !== 'function') {
      return
    }

    host
      .connections()
      .then((value: ConnectionRow[] | { connections?: ConnectionRow[] }) => {
        setConnections(Array.isArray(value) ? value : value.connections || [])
      })
      .catch(() => setConnections([]))
  }, [])

  if (!bot.name.startsWith('handoff-trial-') || typeof host.requestProfile !== 'function') {
    return null
  }

  const call =
    (connectionId: string): HandoffCall =>
    params =>
      host.requestProfile(
        {
          connectionId,
          mode: connectionId === 'local' ? 'local' : 'remote',
          profile: 'default',
          targetProfile: 'default'
        },
        'profiles.handoff',
        { ...params, name: bot.name },
        120_000,
        { spawnPriority: 'foreground' }
      ) as Promise<HandoffState>

  const move = async () => {
    if (!target || busy) {
      return
    }

    setBusy(true)
    setError('')

    try {
      await moveTrialProfile(call(sourceId), call(target))
      await queryClient.invalidateQueries({ queryKey: ROSTER_KEY })

      const migrated: RosterRow = {
        ...bot,
        connectionId: target,
        sourceScoped: true,
        remoteSource: target !== 'local',
        connectionKind: target === 'local' ? 'local' : 'remote',
        route: {
          connectionId: target,
          mode: target === 'local' ? 'local' : 'remote',
          profile: bot.name,
          targetProfile: bot.name
        }
      }

      await openBotCanonicalChat(migrated)
      saveSelectedRosterBot(migrated)
      host.notify({
        kind: 'success',
        message: b.moved
      })
      onComplete()
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : b.failed)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <label htmlFor="profile-execution-target">{b.gateway}</label>
      <Select disabled={busy} onValueChange={setTarget} value={target}>
        <SelectTrigger id="profile-execution-target">
          <SelectValue placeholder={b.destination} />
        </SelectTrigger>
        <SelectContent>
          {connections
            .filter(row => row.id !== sourceId)
            .map(row => (
              <SelectItem key={row.id} value={row.id}>
                {row.label || row.id}
              </SelectItem>
            ))}
        </SelectContent>
      </Select>
      <p className="text-sm text-(--ui-text-tertiary)">{b.hint}</p>
      {error && <p role="alert">{error}</p>}
      <Button disabled={!target || busy} onClick={move} variant="secondary">
        {busy ? b.moving : b.move}
      </Button>
    </div>
  )
}
