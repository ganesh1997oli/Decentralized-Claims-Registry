import type { ClaimStatus, ClaimSummary } from '../api.ts'
import { ipfsUrl, shorten } from '../claim-display.ts'

function statusClasses(status: ClaimStatus): string {
  switch (status) {
    case 'Flagged':
      return 'border-coral/30 bg-coral-pale text-coral-dark'
    case 'Approved':
      return 'border-emerald-200 bg-emerald-50 text-emerald-700'
    case 'Rejected':
      return 'border-red-200 bg-red-50 text-red-700'
    case 'UnderReview':
      return 'border-teal/20 bg-mint text-teal'
    default:
      return 'border-ink/10 bg-sand text-slate'
  }
}

function formatTimestamp(timestamp: number): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(timestamp * 1000))
}

type ClaimsDashboardProps = {
  claims: ClaimSummary[]
  page: number
  pageSize: number
  totalItems: number
  totalPages: number
  isLoading: boolean
  error: string | null
  selectedClaimId: number | null
  openingClaimId: number | null
  onRefresh: () => void
  onClaimSelect: (claim: ClaimSummary) => void
  onPageChange: (page: number) => void
  onPageSizeChange: (pageSize: number) => void
}

export function ClaimsDashboard({
  claims,
  page,
  pageSize,
  totalItems,
  totalPages,
  isLoading,
  error,
  selectedClaimId,
  openingClaimId,
  onRefresh,
  onClaimSelect,
  onPageChange,
  onPageSizeChange,
}: ClaimsDashboardProps) {
  return (
    <section
      id="claims"
      aria-labelledby="claims-title"
      className="mt-10 overflow-hidden rounded-3xl border border-ink/8 bg-white shadow-[0_24px_80px_-48px_rgba(20,40,51,0.38)]"
    >
      <div className="flex flex-col gap-4 border-b border-ink/8 px-6 py-6 sm:flex-row sm:items-end sm:justify-between sm:px-8">
        <div>
          <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">
            Sepolia registry
          </p>
          <h2 id="claims-title" className="mt-1 text-2xl font-bold text-ink">
            All submitted claims
          </h2>
          <p className="mt-1 text-sm leading-6 text-slate">
            Current smart-contract state, newest claim first. Select a claim to
            open its Sepolia and fraud-screening details.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-xs font-semibold text-slate">
            Per page
            <select
              aria-label="Claims per page"
              value={pageSize}
              onChange={(event) => onPageSizeChange(Number(event.target.value))}
              disabled={isLoading}
              className="rounded-full border border-ink/10 bg-white px-3 py-2 font-bold text-ink disabled:cursor-wait disabled:opacity-50"
            >
              {[5, 10, 25, 50].map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>
          <span className="rounded-full bg-sand px-3 py-1.5 text-xs font-bold text-ink">
            {totalItems} {totalItems === 1 ? 'claim' : 'claims'}
          </span>
          <button
            type="button"
            onClick={onRefresh}
            disabled={isLoading}
            className="rounded-full border border-ink/10 bg-white px-4 py-2 text-xs font-bold text-ink transition hover:border-teal hover:text-teal disabled:cursor-wait disabled:opacity-50"
          >
            {isLoading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {error && (
        <div role="alert" className="border-b border-red-200 bg-red-50 px-6 py-3 text-sm font-medium text-red-800 sm:px-8">
          {error}
        </div>
      )}

      {isLoading && claims.length === 0 ? (
        <div className="px-6 py-12 text-center text-sm text-slate sm:px-8">
          Reading claims from Sepolia…
        </div>
      ) : claims.length === 0 ? (
        <div className="px-6 py-12 text-center sm:px-8">
          <p className="font-bold text-ink">No claims have been submitted yet.</p>
          <p className="mt-1 text-sm text-slate">
            Submit the synthetic form above to create the first claim.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-4xl border-collapse text-left">
            <thead className="bg-sand/70 text-xs tracking-[0.12em] text-slate uppercase">
              <tr>
                <th className="px-6 py-3 font-bold sm:px-8">Claim</th>
                <th className="px-4 py-3 font-bold">Status</th>
                <th className="px-4 py-3 font-bold">Fraud score</th>
                <th className="px-4 py-3 font-bold">Claimant</th>
                <th className="px-4 py-3 font-bold">IPFS</th>
                <th className="px-6 py-3 font-bold sm:px-8">Updated</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-ink/8">
              {claims.map((claim) => (
                <tr
                  key={claim.claim_id}
                  className={`align-top transition ${
                    selectedClaimId === claim.claim_id
                      ? 'bg-mint/70'
                      : 'hover:bg-sand/35'
                  }`}
                >
                  <td className="px-6 py-4 sm:px-8">
                    <button
                      type="button"
                      onClick={() => onClaimSelect(claim)}
                      aria-label={`View details for claim ${claim.claim_id}`}
                      aria-current={
                        selectedClaimId === claim.claim_id ? 'true' : undefined
                      }
                      className="group rounded-lg text-left focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-teal"
                    >
                      <span className="block font-bold text-ink group-hover:text-teal">
                        #{claim.claim_id}
                      </span>
                      <span className="mt-1 block font-mono text-xs text-slate">
                        {shorten(claim.claim_hash, 7)}
                      </span>
                      <span className="mt-2 block text-xs font-bold text-teal">
                        {openingClaimId === claim.claim_id
                          ? 'Opening details…'
                          : 'View details →'}
                      </span>
                    </button>
                  </td>
                  <td className="px-4 py-4">
                    <span
                      className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-bold ${statusClasses(claim.status)}`}
                    >
                      {claim.status === 'UnderReview'
                        ? 'Under review'
                        : claim.status}
                    </span>
                  </td>
                  <td className="px-4 py-4">
                    <span className="block font-bold text-ink">
                      {(claim.fraud_score / 100).toFixed(2)}%
                    </span>
                    <span className="mt-1 block text-xs text-slate">
                      {claim.fraud_score.toLocaleString()} / 10,000
                    </span>
                  </td>
                  <td className="px-4 py-4 font-mono text-xs text-slate" title={claim.claimant}>
                    {shorten(claim.claimant, 7)}
                  </td>
                  <td className="px-4 py-4">
                    <a
                      href={ipfsUrl(claim.data_pointer)}
                      target="_blank"
                      rel="noreferrer"
                      title={claim.data_pointer}
                      className="font-mono text-xs font-semibold text-teal underline decoration-teal/25 underline-offset-4 hover:decoration-teal"
                    >
                      {shorten(claim.data_pointer, 7)} ↗
                    </a>
                  </td>
                  <td className="px-6 py-4 text-xs leading-5 text-slate sm:px-8">
                    <span className="block">{formatTimestamp(claim.updated_at)}</span>
                    <span className="mt-1 block text-slate/70">
                      Submitted {formatTimestamp(claim.submitted_at)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex flex-col gap-3 border-t border-ink/8 bg-sand/45 px-6 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-8">
        <p className="text-xs leading-5 text-slate">
          Page {page} of {totalPages}. This prototype reads the requested claims
          directly from the contract; use an indexer at production scale.
        </p>
        <nav aria-label="Claims pagination" className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onPageChange(page - 1)}
            disabled={isLoading || page <= 1}
            className="rounded-full border border-ink/10 bg-white px-4 py-2 text-xs font-bold text-ink transition hover:border-teal hover:text-teal disabled:cursor-not-allowed disabled:opacity-40"
          >
            ← Previous
          </button>
          <span className="min-w-20 text-center text-xs font-bold text-ink">
            {page} / {totalPages}
          </span>
          <button
            type="button"
            onClick={() => onPageChange(page + 1)}
            disabled={isLoading || page >= totalPages}
            className="rounded-full border border-ink/10 bg-white px-4 py-2 text-xs font-bold text-ink transition hover:border-teal hover:text-teal disabled:cursor-not-allowed disabled:opacity-40"
          >
            Next →
          </button>
        </nav>
      </div>
    </section>
  )
}
