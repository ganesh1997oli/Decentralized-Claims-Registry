import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  getIndexerOperations,
  searchIndexerEvents,
  type ClaimIndexEvent,
  type ClaimIndexEventPage,
  type ClaimStatus,
  type IndexerEventSearch,
  type IndexerOperations,
  type IndexerState,
} from '../api.ts'
import { shorten } from '../claim-display.ts'

const OPERATIONS_KEY_SESSION_STORAGE =
  'claims-registry:indexer-operations-key:v1'
const REFRESH_INTERVAL_MS = 15_000
const ETHERSCAN_BASE_URL = 'https://sepolia.etherscan.io'
const TRANSACTION_HASH = /^0x[0-9a-fA-F]{64}$/

const DEFAULT_EVENT_SEARCH: IndexerEventSearch = {
  claimId: null,
  transactionHash: null,
  eventType: null,
  status: null,
  fromBlock: null,
  toBlock: null,
  limit: 20,
}

type EventSearchDraft = {
  identity: string
  eventType: '' | ClaimIndexEvent['event_type']
  status: '' | ClaimStatus
  fromBlock: string
  toBlock: string
  limit: number
}

const EMPTY_EVENT_SEARCH_DRAFT: EventSearchDraft = {
  identity: '',
  eventType: '',
  status: '',
  fromBlock: '',
  toBlock: '',
  limit: 20,
}

function readSessionKey(): string {
  // sessionStorage scopes the raw credential to this browser tab and clears it
  // when the tab closes. A storage-policy exception fails as logged-out rather
  // than preventing the dashboard from rendering.
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
  // Persist only after FastAPI has authenticated the candidate. localStorage is
  // deliberately avoided because it would retain the credential across sessions.
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage?.setItem(OPERATIONS_KEY_SESSION_STORAGE, apiKey)
  } catch {
    // Session persistence is a convenience, never a prerequisite for access.
  }
}

function clearSessionKey(): void {
  // Storage is best-effort; callers also clear React state and active requests,
  // so an unavailable Web Storage API cannot block an in-memory lock operation.
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage?.removeItem(OPERATIONS_KEY_SESSION_STORAGE)
  } catch {
    // A denied storage API must not prevent an in-memory logout.
  }
}

function formatBlock(block: number | null): string {
  // Null is a meaningful degraded/uninitialized state, not block zero.
  return block === null ? 'Unavailable' : block.toLocaleString()
}

function formatDate(value: string | null): string {
  // The API emits ISO timestamps in UTC; Intl presents them in the operator's
  // locale without changing the underlying value used for health decisions.
  if (value === null) return 'Never'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(new Date(value))
}

function formatAge(seconds: number | null): string {
  // Age is supplied by the backend so every browser sees the same classification;
  // this helper only chooses a compact display unit and never recomputes staleness.
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
  // Keep operational semantics from the backend intact while centralizing the
  // label, color, and explanation shown in the summary header.
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
  // This component is intentionally presentation-only. Keeping data derivation in
  // the backend prevents four cards from calculating incompatible health facts.
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

function parseBlockFilter(value: string, label: string): number | null {
  // Inputs remain strings while editing so an empty field is possible. Accept only
  // unsigned decimal integers representable exactly by JavaScript; FastAPI repeats
  // the non-negative constraint because client validation is never authoritative.
  const normalized = value.trim()
  if (!normalized) return null
  if (!/^\d+$/.test(normalized)) {
    throw new Error(`${label} must be a non-negative block number.`)
  }
  const block = Number(normalized)
  if (!Number.isSafeInteger(block)) {
    throw new Error(`${label} is outside the supported integer range.`)
  }
  return block
}

function buildEventSearch(draft: EventSearchDraft): IndexerEventSearch {
  // One identity field accepts either a claim ID or a complete transaction hash.
  // Requiring the full hash preserves an indexed equality query and avoids an
  // expensive/ambiguous substring scan over the immutable audit table.
  const identity = draft.identity.trim()
  let claimId: number | null = null
  let transactionHash: string | null = null
  if (identity) {
    const possibleClaimId = identity.startsWith('#')
      ? identity.slice(1)
      : identity
    if (/^\d+$/.test(possibleClaimId)) {
      claimId = Number(possibleClaimId)
      if (!Number.isSafeInteger(claimId)) {
        throw new Error('Claim ID is outside the supported integer range.')
      }
    } else if (TRANSACTION_HASH.test(identity)) {
      transactionHash = identity.toLowerCase()
    } else {
      throw new Error(
        'Enter a numeric claim ID or a complete 66-character transaction hash.',
      )
    }
  }

  const fromBlock = parseBlockFilter(draft.fromBlock, 'From block')
  const toBlock = parseBlockFilter(draft.toBlock, 'To block')
  if (fromBlock !== null && toBlock !== null && fromBlock > toBlock) {
    throw new Error('From block cannot be greater than to block.')
  }

  return {
    claimId,
    transactionHash,
    eventType: draft.eventType || null,
    status: draft.status || null,
    fromBlock,
    toBlock,
    limit: draft.limit,
  }
}

function EventAuditPanel({
  page,
  pageNumber,
  isLoading,
  error,
  onSearch,
  onOlder,
  onNewer,
}: {
  page: ClaimIndexEventPage | null
  pageNumber: number
  isLoading: boolean
  error: string | null
  onSearch: (filters: IndexerEventSearch) => void
  onOlder: () => void
  onNewer: () => void
}) {
  // Draft form state is separate from the applied search owned by the dashboard.
  // Editing controls therefore cannot change a page until validation succeeds and
  // Search is submitted; pagination always uses one stable applied filter set.
  const [draft, setDraft] = useState<EventSearchDraft>(EMPTY_EVENT_SEARCH_DRAFT)
  const [formError, setFormError] = useState<string | null>(null)
  const events = page?.items ?? []

  function submit(event: FormEvent<HTMLFormElement>) {
    // Convert all draft strings as one operation. A validation failure leaves the
    // current result page and cursor stack untouched for operator comparison.
    event.preventDefault()
    try {
      const filters = buildEventSearch(draft)
      setFormError(null)
      onSearch(filters)
    } catch (validationError) {
      setFormError(
        validationError instanceof Error
          ? validationError.message
          : 'The event filters are invalid.',
      )
    }
  }

  function clear() {
    // Clearing is an applied unfiltered search, not just a visual form reset, so
    // the parent also returns pagination to the newest event page.
    setDraft(EMPTY_EVENT_SEARCH_DRAFT)
    setFormError(null)
    onSearch(DEFAULT_EVENT_SEARCH)
  }

  return (
    <section className="overflow-hidden rounded-3xl border border-ink/8 bg-white">
      <div className="border-b border-ink/8 px-6 py-5 sm:px-8">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">
              Audit stream
            </p>
            <h2 className="mt-1 text-2xl font-bold text-ink">Event explorer</h2>
            <p className="mt-1 text-sm text-slate">
              Search confirmed immutable events without rescanning Sepolia.
            </p>
          </div>
          <span className="w-fit rounded-full bg-mint px-3 py-1.5 text-xs font-bold text-teal">
            Page {pageNumber} · {events.length} shown
          </span>
        </div>
      </div>

      <form onSubmit={submit} className="border-b border-ink/8 bg-sand/55 px-6 py-5 sm:px-8">
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-6">
          <label className="field-group md:col-span-2 xl:col-span-2">
            <span className="field-label">Claim or transaction</span>
            <input
              type="search"
              value={draft.identity}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  identity: event.target.value,
                }))
              }
              className="field-control font-mono"
              placeholder="#6 or full 0x transaction hash"
              disabled={isLoading}
            />
          </label>
          <label className="field-group">
            <span className="field-label">Event</span>
            <select
              value={draft.eventType}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  eventType: event.target.value as EventSearchDraft['eventType'],
                }))
              }
              className="field-control"
              disabled={isLoading}
            >
              <option value="">All events</option>
              <option value="ClaimSubmitted">Submitted</option>
              <option value="ClaimAssessed">Assessed</option>
            </select>
          </label>
          <label className="field-group">
            <span className="field-label">State</span>
            <select
              value={draft.status}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  status: event.target.value as EventSearchDraft['status'],
                }))
              }
              className="field-control"
              disabled={isLoading}
            >
              <option value="">All states</option>
              <option value="Submitted">Submitted</option>
              <option value="UnderReview">Under review</option>
              <option value="Approved">Approved</option>
              <option value="Rejected">Rejected</option>
              <option value="Flagged">Flagged</option>
            </select>
          </label>
          <label className="field-group">
            <span className="field-label">From block</span>
            <input
              inputMode="numeric"
              value={draft.fromBlock}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  fromBlock: event.target.value,
                }))
              }
              className="field-control font-mono"
              placeholder="Oldest"
              disabled={isLoading}
            />
          </label>
          <label className="field-group">
            <span className="field-label">To block</span>
            <input
              inputMode="numeric"
              value={draft.toBlock}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  toBlock: event.target.value,
                }))
              }
              className="field-control font-mono"
              placeholder="Newest"
              disabled={isLoading}
            />
          </label>
        </div>

        <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-xs font-bold text-slate">
              Results
              <select
                aria-label="Events per page"
                value={draft.limit}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    limit: Number(event.target.value),
                  }))
                }
                className="rounded-full border border-ink/10 bg-white px-3 py-2 text-ink"
                disabled={isLoading}
              >
                {[10, 20, 50].map((size) => (
                  <option key={size} value={size}>
                    {size}
                  </option>
                ))}
              </select>
            </label>
            <span className="text-xs leading-5 text-slate">
              New events do not shift an open result page.
            </span>
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={clear}
              disabled={isLoading}
              className="rounded-full border border-ink/10 bg-white px-4 py-2 text-xs font-bold text-ink transition hover:border-coral hover:text-coral-dark disabled:opacity-50"
            >
              Clear
            </button>
            <button
              type="submit"
              disabled={isLoading}
              className="rounded-full bg-ink px-5 py-2 text-xs font-bold text-white transition hover:bg-teal disabled:cursor-wait disabled:opacity-50"
            >
              {isLoading ? 'Searching…' : 'Search events'}
            </button>
          </div>
        </div>
        {(formError || error) && (
          <p role="alert" className="mt-4 text-sm font-medium text-red-700">
            {formError ?? error}
          </p>
        )}
      </form>

      {isLoading && page === null ? (
        <p className="px-6 py-10 text-sm text-slate sm:px-8">
          Searching indexed events…
        </p>
      ) : events.length === 0 ? (
        <div className="px-6 py-10 sm:px-8">
          <p className="font-bold text-ink">No matching events</p>
          <p className="mt-1 text-sm text-slate">
            Clear one or more filters, or verify the claim and block values.
          </p>
        </div>
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
              {events.map((event) => (
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
                      title={event.transaction_hash}
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

      <div className="flex items-center justify-between gap-3 border-t border-ink/8 bg-sand/35 px-6 py-4 sm:px-8">
        <p className="text-xs text-slate">
          Keyset page {pageNumber}; newest matching events appear first.
        </p>
        <nav aria-label="Event search pagination" className="flex gap-2">
          <button
            type="button"
            onClick={onNewer}
            disabled={isLoading || pageNumber <= 1}
            className="rounded-full border border-ink/10 bg-white px-4 py-2 text-xs font-bold text-ink transition hover:border-teal hover:text-teal disabled:cursor-not-allowed disabled:opacity-40"
          >
            ← Newer
          </button>
          <button
            type="button"
            onClick={onOlder}
            disabled={isLoading || page?.next_cursor === null || page === null}
            className="rounded-full border border-ink/10 bg-white px-4 py-2 text-xs font-bold text-ink transition hover:border-teal hover:text-teal disabled:cursor-not-allowed disabled:opacity-40"
          >
            Older →
          </button>
        </nav>
      </div>
    </section>
  )
}

/**
 * Render an already-authenticated telemetry snapshot and event page.
 *
 * The component performs no network or credential work. Keeping it pure makes
 * server-rendered regression tests deterministic and prevents presentation code
 * from creating a second interpretation of backend health state.
 */
export function IndexerOperationsView({
  snapshot,
  isRefreshing,
  error,
  eventPage,
  eventPageNumber,
  isSearchingEvents,
  eventError,
  onRefresh,
  onDisconnect,
  onEventSearch,
  onOlderEvents,
  onNewerEvents,
}: {
  snapshot: IndexerOperations
  isRefreshing: boolean
  error: string | null
  eventPage: ClaimIndexEventPage | null
  eventPageNumber: number
  isSearchingEvents: boolean
  eventError: string | null
  onRefresh: () => void
  onDisconnect: () => void
  onEventSearch: (filters: IndexerEventSearch) => void
  onOlderEvents: () => void
  onNewerEvents: () => void
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

      <EventAuditPanel
        page={eventPage}
        pageNumber={eventPageNumber}
        isLoading={isSearchingEvents}
        error={eventError}
        onSearch={onEventSearch}
        onOlder={onOlderEvents}
        onNewer={onNewerEvents}
      />
    </div>
  )
}

/**
 * Own the credential gate, refresh lifecycle, event search, and cursor history.
 *
 * Raw credentials stay in memory/sessionStorage and are passed only to the API
 * client. Telemetry polling and event searches use independent abort controllers
 * because a slow RPC sample must not block PostgreSQL-only audit exploration.
 */
export function IndexerOperationsDashboard() {
  const [draftKey, setDraftKey] = useState('')
  const [activeKey, setActiveKey] = useState(readSessionKey)
  const [snapshot, setSnapshot] = useState<IndexerOperations | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [eventPage, setEventPage] = useState<ClaimIndexEventPage | null>(null)
  const [eventFilters, setEventFilters] =
    useState<IndexerEventSearch>(DEFAULT_EVENT_SEARCH)
  const [eventCursors, setEventCursors] = useState<(string | null)[]>([null])
  const [eventPageNumber, setEventPageNumber] = useState(1)
  const [eventError, setEventError] = useState<string | null>(null)
  const [isSearchingEvents, setIsSearchingEvents] = useState(false)
  const request = useRef<AbortController | null>(null)
  const eventRequest = useRef<AbortController | null>(null)

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

  const loadEvents = useCallback(
    async (
      apiKey: string,
      filters: IndexerEventSearch,
      cursor: string | null,
    ) => {
      // Search interactions replace the prior event request. Unlike telemetry
      // polling, the latest filter selection is the only result worth keeping.
      eventRequest.current?.abort()
      const controller = new AbortController()
      eventRequest.current = controller
      setIsSearchingEvents(true)
      setEventError(null)
      try {
        const result = await searchIndexerEvents(
          apiKey,
          filters,
          cursor,
          controller.signal,
        )
        if (!controller.signal.aborted) setEventPage(result)
      } catch (loadingError) {
        if (controller.signal.aborted) return
        const message =
          loadingError instanceof Error
            ? loadingError.message
            : 'Indexer events could not be searched.'
        setEventError(message)
        if (message.toLowerCase().includes('operations api key')) {
          clearSessionKey()
          setActiveKey('')
          setSnapshot(null)
          setEventPage(null)
        }
      } finally {
        if (eventRequest.current === controller) eventRequest.current = null
        if (!controller.signal.aborted) setIsSearchingEvents(false)
      }
    },
    [],
  )

  useEffect(() => {
    // Poll only while authenticated and visible. The currently rendered snapshot
    // remains available if a later refresh fails, and cleanup aborts work when the
    // credential changes or the component unmounts.
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

  useEffect(() => {
    // A newly authenticated key begins one independent event-query lifecycle at
    // the newest unfiltered page. Disconnect/unmount aborts any in-flight search.
    if (!activeKey) return
    setEventFilters(DEFAULT_EVENT_SEARCH)
    setEventCursors([null])
    setEventPageNumber(1)
    setEventPage(null)
    void loadEvents(activeKey, DEFAULT_EVENT_SEARCH, null)
    return () => {
      eventRequest.current?.abort()
      eventRequest.current = null
    }
  }, [activeKey, loadEvents])

  function connect(event: FormEvent<HTMLFormElement>) {
    // Do not activate or persist a candidate until a real protected request has
    // succeeded. This catches both an invalid key and a backend using an old digest.
    event.preventDefault()
    const candidate = draftKey.trim()
    if (!candidate) {
      setError('Enter the operations API key.')
      return
    }
    void load(candidate, true)
  }

  function disconnect() {
    // Abort both request classes before erasing all credential-derived state. This
    // prevents a late response from repopulating the locked dashboard.
    request.current?.abort()
    request.current = null
    eventRequest.current?.abort()
    eventRequest.current = null
    clearSessionKey()
    setActiveKey('')
    setSnapshot(null)
    setError(null)
    setIsRefreshing(false)
    setEventPage(null)
    setEventFilters(DEFAULT_EVENT_SEARCH)
    setEventCursors([null])
    setEventPageNumber(1)
    setEventError(null)
    setIsSearchingEvents(false)
  }

  function searchEvents(filters: IndexerEventSearch) {
    // Applying different filters invalidates every old cursor because cursors are
    // positions within a particular ordered result set, not global page numbers.
    setEventFilters(filters)
    setEventCursors([null])
    setEventPageNumber(1)
    setEventPage(null)
    void loadEvents(activeKey, filters, null)
  }

  function showOlderEvents() {
    // The server-provided cursor names the first position strictly older than this
    // page. Store it at the next page index before requesting that older slice.
    const cursor = eventPage?.next_cursor
    if (!cursor) return
    const nextPageNumber = eventPageNumber + 1
    // Retain the cursor that opened each page. This provides a deterministic
    // client-side "Newer" action without asking PostgreSQL for reverse pages.
    setEventCursors((current) => [
      ...current.slice(0, eventPageNumber),
      cursor,
    ])
    setEventPageNumber(nextPageNumber)
    void loadEvents(activeKey, eventFilters, cursor)
  }

  function showNewerEvents() {
    // PostgreSQL exposes only forward (older) keyset traversal. Returning newer is
    // deterministic because the browser retains the cursor that opened each page;
    // page one uses null to mean the current newest matching row.
    if (eventPageNumber <= 1) return
    const previousPageNumber = eventPageNumber - 1
    const cursor = eventCursors[previousPageNumber - 1] ?? null
    setEventPageNumber(previousPageNumber)
    void loadEvents(activeKey, eventFilters, cursor)
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
      eventPage={eventPage}
      eventPageNumber={eventPageNumber}
      isSearchingEvents={isSearchingEvents}
      eventError={eventError}
      onRefresh={() => void load(activeKey)}
      onDisconnect={disconnect}
      onEventSearch={searchEvents}
      onOlderEvents={showOlderEvents}
      onNewerEvents={showNewerEvents}
    />
  )
}
