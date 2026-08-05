import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  getIndexerOperations,
  type IndexerOperations,
  type IndexerState,
} from '../api.ts'
import { shorten } from '../claim-display.ts'

const OPERATIONS_KEY_SESSION_STORAGE =
  'claims-registry:indexer-operations-key:v1'
const REFRESH_INTERVAL_MS = 15_000
const ETHERSCAN_BASE_URL = 'https://sepolia.etherscan.io'

function readSessionKey(): string {
  if (typeof window === 'undefined') return ''
  try {
    return window.sessionStorage?.getItem(OPERATIONS_KEY_SESSION_STORAGE) ?? ''
  } catch {
    // Private browsing policies may disable storage. The dashboard can still
    // operate for this render; it will simply ask for the key again on reload.
    return ''
  }
}

function storeSessionKey(apiKey: string): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage?.setItem(OPERATIONS_KEY_SESSION_STORAGE, apiKey)
  } catch {
    // Session persistence is a convenience, never a prerequisite for access.
  }
}

function clearSessionKey(): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage?.removeItem(OPERATIONS_KEY_SESSION_STORAGE)
  } catch {
    // A denied storage API must not prevent an in-memory logout.
  }
}

function formatBlock(block: number | null): string {
  return block === null ? 'Unavailable' : block.toLocaleString()
}

function formatDate(value: string | null): string {
  if (value === null) return 'Never'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(new Date(value))
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return 'Unavailable'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3_600)}h ago`
}

function statePresentation(state: IndexerState): {
  label: string
  classes: string
  explanation: string
} {
  switch (state) {
    case 'healthy':
      return {
        label: 'Healthy',
        classes: 'border-emerald-200 bg-emerald-50 text-emerald-700',
        explanation: 'The durable checkpoint has reached the confirmed head.',
      }
    case 'catching_up':
      return {
        label: 'Catching up',
        classes: 'border-sky-200 bg-sky-50 text-sky-700',
        explanation: 'The checkpoint is advancing toward the confirmed head.',
      }
    case 'stalled':
      return {
        label: 'Stalled',
        classes: 'border-red-200 bg-red-50 text-red-700',
        explanation: 'The index is behind and its checkpoint has stopped moving.',
      }
    case 'uninitialized':
      return {
        label: 'Uninitialized',
        classes: 'border-amber-200 bg-amber-50 text-amber-800',
        explanation: 'No durable checkpoint exists for this deployment.',
      }
    default:
      return {
        label: 'Degraded',
        classes: 'border-orange-200 bg-orange-50 text-orange-800',
        explanation: 'Some telemetry is available, but the chain RPC check failed.',
      }
  }
}

function MetricCard({
  label,
  value,
  detail,
}: {
  label: string
  value: string
  detail: string
}) {
  return (
    <article className="rounded-2xl border border-ink/8 bg-white p-5 shadow-sm">
      <p className="text-xs font-bold tracking-[0.14em] text-slate uppercase">
        {label}
      </p>
      <p className="mt-3 font-mono text-2xl font-black tracking-[-0.03em] text-ink">
        {value}
      </p>
      <p className="mt-2 text-xs leading-5 text-slate">{detail}</p>
    </article>
  )
}

/** Pure snapshot presentation exported for server-rendered regression tests. */
export function IndexerOperationsView({
  snapshot,
  isRefreshing,
  error,
  onRefresh,
  onDisconnect,
}: {
  snapshot: IndexerOperations
  isRefreshing: boolean
  error: string | null
  onRefresh: () => void
  onDisconnect: () => void
}) {
  const state = statePresentation(snapshot.state)
  const statusRows = [
    ['Submitted', snapshot.claim_status_counts.submitted, 'bg-slate'],
    ['Under review', snapshot.claim_status_counts.under_review, 'bg-teal'],
    ['Approved', snapshot.claim_status_counts.approved, 'bg-emerald-500'],
    ['Rejected', snapshot.claim_status_counts.rejected, 'bg-red-500'],
    ['Flagged', snapshot.claim_status_counts.flagged, 'bg-coral'],
  ] as const

  return (
    <div className="space-y-8">
      <section className="overflow-hidden rounded-3xl border border-ink/8 bg-white shadow-[0_24px_80px_-48px_rgba(20,40,51,0.38)]">
        <div className="flex flex-col gap-5 border-b border-ink/8 px-6 py-6 sm:flex-row sm:items-start sm:justify-between sm:px-8">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <span
                className={`inline-flex rounded-full border px-3 py-1 text-xs font-bold ${state.classes}`}
              >
                {state.label}
              </span>
              <span className="text-xs font-semibold text-slate">
                RPC {snapshot.rpc_status}
              </span>
            </div>
            <h1 className="mt-4 text-3xl font-black tracking-[-0.03em] text-ink sm:text-4xl">
              Blockchain indexer operations
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-slate">
              {state.explanation} This page is read-only and refreshes every 15
              seconds while it remains open.
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              onClick={onRefresh}
              disabled={isRefreshing}
              className="rounded-full bg-ink px-4 py-2 text-xs font-bold text-white transition hover:bg-teal disabled:cursor-wait disabled:opacity-50"
            >
              {isRefreshing ? 'Refreshing…' : 'Refresh now'}
            </button>
            <button
              type="button"
              onClick={onDisconnect}
              className="rounded-full border border-ink/10 bg-white px-4 py-2 text-xs font-bold text-ink transition hover:border-coral hover:text-coral-dark"
            >
              Lock
            </button>
          </div>
        </div>

        {error && (
          <div
            role="alert"
            className="border-b border-red-200 bg-red-50 px-6 py-3 text-sm font-medium text-red-800 sm:px-8"
          >
            The last refresh failed: {error}. Previously loaded telemetry remains
            visible.
          </div>
        )}

        <dl className="grid gap-px bg-ink/8 sm:grid-cols-2 lg:grid-cols-4">
          <div className="bg-white px-6 py-5 sm:px-8 lg:px-6">
            <dt className="text-xs font-bold tracking-[0.12em] text-slate uppercase">
              Deployment
            </dt>
            <dd className="mt-2 text-sm font-bold text-ink">
              {snapshot.deployment_id}
            </dd>
          </div>
          <div className="bg-white px-6 py-5 lg:px-6">
            <dt className="text-xs font-bold tracking-[0.12em] text-slate uppercase">
              Chain
            </dt>
            <dd className="mt-2 text-sm font-bold text-ink">
              Sepolia · {snapshot.chain_id}
            </dd>
          </div>
          <div className="bg-white px-6 py-5 lg:px-6">
            <dt className="text-xs font-bold tracking-[0.12em] text-slate uppercase">
              Contract
            </dt>
            <dd className="mt-2 font-mono text-sm font-bold text-teal">
              <a
                href={`${ETHERSCAN_BASE_URL}/address/${snapshot.contract_address}`}
                target="_blank"
                rel="noreferrer"
                title={snapshot.contract_address}
                className="underline decoration-teal/25 underline-offset-4 hover:decoration-teal"
              >
                {shorten(snapshot.contract_address, 8)} ↗
              </a>
            </dd>
          </div>
          <div className="bg-white px-6 py-5 sm:px-8 lg:px-6">
            <dt className="text-xs font-bold tracking-[0.12em] text-slate uppercase">
              Observed
            </dt>
            <dd className="mt-2 text-sm font-bold text-ink">
              {formatDate(snapshot.observed_at)}
            </dd>
          </div>
        </dl>
      </section>

      <section aria-labelledby="progress-title">
        <div className="mb-4">
          <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">
            Progress
          </p>
          <h2 id="progress-title" className="mt-1 text-2xl font-bold text-ink">
            Chain and checkpoint
          </h2>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Latest block"
            value={formatBlock(snapshot.latest_block)}
            detail="Newest block reported by the configured Sepolia RPC."
          />
          <MetricCard
            label="Confirmed head"
            value={formatBlock(snapshot.safe_block)}
            detail={`${snapshot.confirmation_blocks} confirmation blocks behind the latest head.`}
          />
          <MetricCard
            label="Indexed through"
            value={formatBlock(snapshot.indexed_through_block)}
            detail={`Checkpoint updated ${formatAge(snapshot.checkpoint_age_seconds)}.`}
          />
          <MetricCard
            label="Block lag"
            value={formatBlock(snapshot.block_lag)}
            detail={`Marked stalled after ${snapshot.stale_after_seconds}s without progress while behind.`}
          />
        </div>
      </section>

      <section className="grid gap-6 lg:grid-cols-[1.15fr_0.85fr]">
        <div className="rounded-3xl border border-ink/8 bg-white p-6 sm:p-8">
          <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">
            Projection
          </p>
          <h2 className="mt-1 text-2xl font-bold text-ink">Indexed workload</h2>
          <div className="mt-6 grid grid-cols-3 gap-3">
            {[
              ['Claims', snapshot.total_claims],
              ['Events', snapshot.total_events],
              ['Assessments', snapshot.assessed_events],
            ].map(([label, value]) => (
              <div key={label} className="rounded-2xl bg-sand p-4">
                <span className="block font-mono text-2xl font-black text-ink">
                  {Number(value).toLocaleString()}
                </span>
                <span className="mt-1 block text-xs font-bold text-slate">
                  {label}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-7 space-y-4">
            {statusRows.map(([label, count, barClass]) => {
              const percentage =
                snapshot.total_claims === 0
                  ? 0
                  : Math.round((count / snapshot.total_claims) * 100)
              return (
                <div key={label}>
                  <div className="flex items-center justify-between text-xs font-bold">
                    <span className="text-ink">{label}</span>
                    <span className="text-slate">
                      {count.toLocaleString()} · {percentage}%
                    </span>
                  </div>
                  <div className="mt-2 h-2 overflow-hidden rounded-full bg-sand">
                    <div
                      className={`h-full rounded-full ${barClass}`}
                      style={{ width: `${percentage}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        <div className="rounded-3xl border border-ink/8 bg-ink p-6 text-white sm:p-8">
          <p className="text-xs font-bold tracking-[0.16em] text-coral uppercase">
            Reconciliation
          </p>
          <h2 className="mt-1 text-2xl font-bold">Last contract comparison</h2>
          {snapshot.last_reconciliation ? (
            <div className="mt-6">
              <span
                className={`inline-flex rounded-full border px-3 py-1 text-xs font-bold ${
                  snapshot.last_reconciliation.consistent
                    ? 'border-emerald-300/30 bg-emerald-300/10 text-emerald-200'
                    : 'border-red-300/30 bg-red-300/10 text-red-200'
                }`}
              >
                {snapshot.last_reconciliation.consistent
                  ? 'Consistent'
                  : 'Mismatch detected'}
              </span>
              <dl className="mt-6 space-y-4 text-sm">
                <div className="flex justify-between gap-4 border-b border-white/10 pb-4">
                  <dt className="text-white/55">Checked</dt>
                  <dd className="text-right font-bold">
                    {formatDate(snapshot.last_reconciliation.checked_at)}
                  </dd>
                </div>
                <div className="flex justify-between gap-4 border-b border-white/10 pb-4">
                  <dt className="text-white/55">Snapshot block</dt>
                  <dd className="font-mono font-bold">
                    {snapshot.last_reconciliation.indexed_through_block.toLocaleString()}
                  </dd>
                </div>
                <div className="flex justify-between gap-4 border-b border-white/10 pb-4">
                  <dt className="text-white/55">Contract / index</dt>
                  <dd className="font-bold">
                    {snapshot.last_reconciliation.chain_claims} /{' '}
                    {snapshot.last_reconciliation.indexed_claims}
                  </dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-white/55">Missing / stale / unexpected</dt>
                  <dd className="font-bold">
                    {snapshot.last_reconciliation.missing_claim_ids.length} /{' '}
                    {snapshot.last_reconciliation.mismatched_claim_ids.length} /{' '}
                    {snapshot.last_reconciliation.unexpected_claim_ids.length}
                  </dd>
                </div>
              </dl>
            </div>
          ) : (
            <div className="mt-6 rounded-2xl border border-white/10 bg-white/5 p-5">
              <p className="font-bold">No reconciliation has been recorded.</p>
              <p className="mt-2 text-sm leading-6 text-white/55">
                After the listener catches up, stop it briefly and run the
                reconciliation command. Its next result will appear here.
              </p>
            </div>
          )}
          <p className="mt-6 text-xs leading-5 text-white/45">
            The dashboard cannot run repairs or rebuilds. Those operations remain
            an explicit, reviewed CLI procedure.
          </p>
        </div>
      </section>

      <section className="overflow-hidden rounded-3xl border border-ink/8 bg-white">
        <div className="border-b border-ink/8 px-6 py-5 sm:px-8">
          <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">
            Audit stream
          </p>
          <h2 className="mt-1 text-2xl font-bold text-ink">Recent events</h2>
          <p className="mt-1 text-sm text-slate">
            Newest confirmed immutable events indexed for this deployment.
          </p>
        </div>
        {snapshot.recent_events.length === 0 ? (
          <p className="px-6 py-10 text-sm text-slate sm:px-8">
            No events have been indexed yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-3xl border-collapse text-left">
              <thead className="bg-sand/70 text-xs tracking-[0.12em] text-slate uppercase">
                <tr>
                  <th className="px-6 py-3 font-bold sm:px-8">Event</th>
                  <th className="px-4 py-3 font-bold">Claim</th>
                  <th className="px-4 py-3 font-bold">Block</th>
                  <th className="px-4 py-3 font-bold">State</th>
                  <th className="px-6 py-3 font-bold sm:px-8">Indexed</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink/8">
                {snapshot.recent_events.map((event) => (
                  <tr key={event.event_id} className="hover:bg-sand/35">
                    <td className="px-6 py-4 sm:px-8">
                      <span className="block text-sm font-bold text-ink">
                        {event.event_type === 'ClaimSubmitted'
                          ? 'Claim submitted'
                          : 'Claim assessed'}
                      </span>
                      <a
                        href={`${ETHERSCAN_BASE_URL}/tx/${event.transaction_hash}`}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-1 block font-mono text-xs text-teal underline decoration-teal/25 underline-offset-4"
                      >
                        {shorten(event.transaction_hash, 8)} ↗
                      </a>
                    </td>
                    <td className="px-4 py-4 font-mono text-sm font-bold text-ink">
                      #{event.claim_id}
                    </td>
                    <td className="px-4 py-4 font-mono text-xs text-slate">
                      {event.block_number.toLocaleString()}:{event.log_index}
                    </td>
                    <td className="px-4 py-4 text-sm">
                      <span className="block font-bold text-ink">
                        {event.status === 'UnderReview'
                          ? 'Under review'
                          : event.status}
                      </span>
                      <span className="mt-1 block text-xs text-slate">
                        {(event.fraud_score / 100).toFixed(2)}% score
                      </span>
                    </td>
                    <td className="px-6 py-4 text-xs text-slate sm:px-8">
                      {formatDate(event.indexed_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}

/** Credential gate and polling lifecycle for the operations snapshot. */
export function IndexerOperationsDashboard() {
  const [draftKey, setDraftKey] = useState('')
  const [activeKey, setActiveKey] = useState(readSessionKey)
  const [snapshot, setSnapshot] = useState<IndexerOperations | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const request = useRef<AbortController | null>(null)

  const load = useCallback(async (apiKey: string, persistOnSuccess = false) => {
    // A slow RPC sample must not create overlapping browser requests every time
    // the refresh interval fires. The existing request owns this cycle; the
    // next scheduled or manual refresh can sample again after it finishes.
    if (request.current) return
    const controller = new AbortController()
    request.current = controller
    setIsRefreshing(true)
    setError(null)
    try {
      const result = await getIndexerOperations(apiKey, controller.signal)
      if (controller.signal.aborted) return
      setSnapshot(result)
      if (persistOnSuccess) {
        storeSessionKey(apiKey)
        setActiveKey(apiKey)
        setDraftKey('')
      }
    } catch (loadingError) {
      if (controller.signal.aborted) return
      const message =
        loadingError instanceof Error
          ? loadingError.message
          : 'Indexer operations could not be loaded.'
      setError(message)
      if (message.toLowerCase().includes('operations api key')) {
        clearSessionKey()
        setActiveKey('')
        setSnapshot(null)
      }
    } finally {
      if (request.current === controller) request.current = null
      if (!controller.signal.aborted) setIsRefreshing(false)
    }
  }, [])

  useEffect(() => {
    if (!activeKey) return
    // A successful unlock already supplied the first snapshot. A restored
    // session key does not, so only that path needs an immediate request.
    if (snapshot === null) void load(activeKey)
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load(activeKey)
    }, REFRESH_INTERVAL_MS)
    return () => {
      window.clearInterval(interval)
      request.current?.abort()
      request.current = null
    }
  }, [activeKey, load, snapshot])

  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const candidate = draftKey.trim()
    if (!candidate) {
      setError('Enter the operations API key.')
      return
    }
    void load(candidate, true)
  }

  function disconnect() {
    request.current?.abort()
    request.current = null
    clearSessionKey()
    setActiveKey('')
    setSnapshot(null)
    setError(null)
    setIsRefreshing(false)
  }

  if (!activeKey && !snapshot) {
    return (
      <section className="mx-auto max-w-xl rounded-3xl border border-ink/8 bg-white p-6 shadow-[0_24px_80px_-48px_rgba(20,40,51,0.38)] sm:p-8">
        <span className="grid size-12 place-items-center rounded-2xl bg-mint text-xl text-teal">
          ◈
        </span>
        <p className="mt-6 text-xs font-bold tracking-[0.16em] text-teal uppercase">
          Restricted operations
        </p>
        <h1 className="mt-2 text-3xl font-black tracking-[-0.03em] text-ink">
          Unlock indexer telemetry
        </h1>
        <p className="mt-3 text-sm leading-6 text-slate">
          Enter the operations API key. It is sent only in a request header and
          retained in this browser tab for the current session.
        </p>
        <form onSubmit={connect} className="mt-7 space-y-4">
          <label className="field-group">
            <span className="field-label">Operations API key</span>
            <input
              type="password"
              autoComplete="current-password"
              value={draftKey}
              onChange={(event) => setDraftKey(event.target.value)}
              className="field-control font-mono"
              placeholder="Paste operator key"
              disabled={isRefreshing}
            />
          </label>
          {error && (
            <p role="alert" className="text-sm font-medium text-red-700">
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={isRefreshing}
            className="w-full rounded-xl bg-ink px-5 py-3 text-sm font-bold text-white transition hover:bg-teal disabled:cursor-wait disabled:opacity-50"
          >
            {isRefreshing ? 'Verifying…' : 'Open operations dashboard'}
          </button>
        </form>
        <p className="mt-5 text-xs leading-5 text-slate">
          No index reset, replay, or repair operation is exposed through this
          page.
        </p>
      </section>
    )
  }

  if (!snapshot) {
    return (
      <div className="py-20 text-center text-sm font-semibold text-slate">
        Loading authenticated indexer telemetry…
      </div>
    )
  }

  return (
    <IndexerOperationsView
      snapshot={snapshot}
      isRefreshing={isRefreshing}
      error={error}
      onRefresh={() => void load(activeKey)}
      onDisconnect={disconnect}
    />
  )
}
