import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  getClaimAssessment,
  getGovernanceSession,
  listClaims,
  prepareCoverageDecision,
  type ClaimAssessment,
  type ClaimSummary,
  type CoverageDecisionProposal,
  type CoverageDecisionStatus,
  type GovernanceSession,
} from '../api.ts'
import {
  connectWallet,
  sendCoverageDecisionTransaction,
} from '../wallet.ts'
import { shorten } from '../claim-display.ts'

const GOVERNANCE_KEY_STORAGE = 'claims-registry:governance-key:v1'

function readSessionKey(): string {
  if (typeof window === 'undefined') return ''
  try {
    return window.sessionStorage?.getItem(GOVERNANCE_KEY_STORAGE) ?? ''
  } catch {
    return ''
  }
}

function persistSessionKey(value: string): void {
  try {
    window.sessionStorage?.setItem(GOVERNANCE_KEY_STORAGE, value)
  } catch {
    // In-memory authentication still works when storage is disabled.
  }
}

function clearSessionKey(): void {
  try {
    window.sessionStorage?.removeItem(GOVERNANCE_KEY_STORAGE)
  } catch {
    // React state is cleared below even if the browser blocks storage access.
  }
}

/**
 * Maker/checker console for final insurer coverage decisions.
 *
 * The API key identifies the operator who prepares the immutable proposal. A
 * different authority—the connected DECISION_MAKER_ROLE wallet—must visibly
 * approve and pay for the Sepolia transaction. Neither credential can complete
 * the workflow alone.
 */
export function CoverageGovernanceDashboard() {
  const [apiKey, setApiKey] = useState(() => readSessionKey())
  const [apiKeyDraft, setApiKeyDraft] = useState(() => readSessionKey())
  const [session, setSession] = useState<GovernanceSession | null>(null)
  const [claims, setClaims] = useState<ClaimSummary[]>([])
  const [selected, setSelected] = useState<ClaimSummary | null>(null)
  const [assessment, setAssessment] = useState<ClaimAssessment | null>(null)
  const [walletAddress, setWalletAddress] = useState('')
  const [decision, setDecision] = useState<CoverageDecisionStatus>('Approved')
  const [proposal, setProposal] = useState<CoverageDecisionProposal | null>(null)
  const [transactionHash, setTransactionHash] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!apiKey || session) return
    getGovernanceSession(apiKey)
      .then(setSession)
      .catch((reason: unknown) => {
        clearSessionKey()
        setApiKey('')
        setError(reason instanceof Error ? reason.message : 'Authentication failed.')
      })
  }, [apiKey, session])

  useEffect(() => {
    if (!session) return
    const controller = new AbortController()
    setBusy(true)
    listClaims(1, 50, controller.signal)
      .then((page) => {
        // Terminal and not-yet-screened claims are visible in the public app but
        // cannot enter this queue. The backend repeats this rule authoritatively.
        const eligible = page.items.filter(
          (claim) => claim.status === 'UnderReview' || claim.status === 'Flagged',
        )
        setClaims(eligible)
        setSelected((current) => current ?? eligible[0] ?? null)
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setError(reason instanceof Error ? reason.message : 'Could not load claims.')
      })
      .finally(() => setBusy(false))
    return () => controller.abort()
  }, [session])

  useEffect(() => {
    if (!selected) return
    const controller = new AbortController()
    setProposal(null)
    setTransactionHash(null)
    getClaimAssessment(selected.claim_id, controller.signal)
      .then(setAssessment)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setError(reason instanceof Error ? reason.message : 'Could not load screening.')
      })
    return () => controller.abort()
  }, [selected])

  async function unlock(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const candidate = apiKeyDraft.trim()
    if (!candidate) return
    setBusy(true)
    setError(null)
    try {
      const authenticated = await getGovernanceSession(candidate)
      persistSessionKey(candidate)
      setApiKey(candidate)
      setSession(authenticated)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Authentication failed.')
    } finally {
      setBusy(false)
    }
  }

  function lock() {
    clearSessionKey()
    setApiKey('')
    setApiKeyDraft('')
    setSession(null)
    setClaims([])
    setSelected(null)
    setWalletAddress('')
    setProposal(null)
    setTransactionHash(null)
    setError(null)
  }

  async function connectDecisionWallet() {
    setBusy(true)
    setError(null)
    try {
      setWalletAddress(await connectWallet())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Wallet connection failed.')
    } finally {
      setBusy(false)
    }
  }

  async function prepareAndSend() {
    if (!selected || !walletAddress || !apiKey) return
    setBusy(true)
    setError(null)
    setProposal(null)
    setTransactionHash(null)
    try {
      const prepared = await prepareCoverageDecision(
        selected.claim_id,
        decision,
        walletAddress,
        apiKey,
      )
      setProposal(prepared)
      // Wallet confirmation is deliberately a second, visible action. The API
      // response alone cannot finalize the decision or spend from this account.
      const hash = await sendCoverageDecisionTransaction(prepared)
      setTransactionHash(hash)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Decision failed.')
    } finally {
      setBusy(false)
    }
  }

  if (!session) {
    return (
      <section className="mx-auto max-w-xl rounded-3xl border border-ink/8 bg-white p-8">
        <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">Private governance</p>
        <h1 className="mt-2 text-3xl font-black text-ink">Coverage decision console</h1>
        <p className="mt-3 text-sm leading-6 text-slate">
          The governance key prepares an attributable proposal. It cannot replace the separately scoped decision wallet.
        </p>
        <form onSubmit={unlock} className="mt-6">
          <label htmlFor="governance-key" className="text-sm font-bold text-ink">Governance API key</label>
          <input id="governance-key" type="password" autoComplete="off" value={apiKeyDraft} onChange={(event) => setApiKeyDraft(event.target.value)} className="mt-2 w-full rounded-xl border border-ink/15 px-4 py-3 font-mono text-sm" />
          {error ? <p role="alert" className="mt-3 text-sm text-red-700">{error}</p> : null}
          <button type="submit" disabled={busy || !apiKeyDraft.trim()} className="mt-5 rounded-full bg-ink px-5 py-2.5 text-sm font-bold text-white disabled:opacity-50">{busy ? 'Checking…' : 'Unlock governance'}</button>
        </form>
      </section>
    )
  }

  return (
    <div className="space-y-6">
      <section className="flex flex-wrap items-center justify-between gap-4 rounded-3xl bg-ink px-6 py-5 text-white">
        <div>
          <p className="text-xs font-bold tracking-[0.16em] text-coral uppercase">Coverage governance</p>
          <h1 className="mt-1 text-2xl font-black">{session.governance_reference}</h1>
          <p className="mt-1 font-mono text-xs text-white/60">Insurer {shorten(session.insurer_address, 8)}</p>
        </div>
        <button type="button" onClick={lock} className="rounded-full border border-white/15 px-4 py-2 text-xs font-bold">Lock console</button>
      </section>

      {error ? <p role="alert" className="rounded-2xl border border-red-200 bg-red-50 px-5 py-3 text-sm text-red-800">{error}</p> : null}

      <div className="grid gap-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
        <section className="overflow-hidden rounded-3xl border border-ink/8 bg-white">
          <div className="border-b border-ink/8 px-5 py-4"><p className="text-xs font-bold tracking-[0.14em] text-teal uppercase">Decision queue</p></div>
          <ul className="divide-y divide-ink/8">
            {claims.map((claim) => (
              <li key={claim.claim_id}>
                <button type="button" onClick={() => setSelected(claim)} className={`w-full px-5 py-4 text-left ${selected?.claim_id === claim.claim_id ? 'bg-mint' : 'hover:bg-sand/50'}`}>
                  <span className="font-bold text-ink">Claim #{claim.claim_id}</span>
                  <span className="mt-1 block text-xs text-slate">{claim.status} · {(claim.fraud_score / 100).toFixed(2)}%</span>
                </button>
              </li>
            ))}
          </ul>
          {!claims.length && !busy ? <p className="p-5 text-sm text-slate">No screened claims await a decision.</p> : null}
        </section>

        {selected ? (
          <section className="rounded-3xl border border-ink/8 bg-white p-6 sm:p-8">
            <p className="text-xs font-bold tracking-[0.14em] text-teal uppercase">Maker / checker decision</p>
            <h2 className="mt-1 text-2xl font-black text-ink">Claim #{selected.claim_id}</h2>
            <div className="mt-5 rounded-2xl bg-sand p-4 text-sm text-slate">
              Model: <strong className="text-ink">{assessment?.status ?? 'loading'}</strong> · {assessment ? `${(assessment.probability * 100).toFixed(1)}% probability` : 'screening unavailable'}
            </div>
            <p className="mt-4 text-sm leading-6 text-slate">
              FastAPI also requires a completed on-chain screening and the latest conclusive human review. Notes remain private and are not written to Sepolia; their revision is bound into the decision hash.
            </p>

            <div className="mt-6 grid gap-3 sm:grid-cols-2">
              {(['Approved', 'Rejected'] as CoverageDecisionStatus[]).map((status) => (
                <label key={status} className={`rounded-2xl border p-4 font-bold ${decision === status ? 'border-teal bg-mint text-teal' : 'border-ink/10 text-ink'}`}>
                  <input type="radio" name="decision" value={status} checked={decision === status} onChange={() => setDecision(status)} className="mr-2" />
                  {status}
                </label>
              ))}
            </div>

            <div className="mt-6 flex flex-wrap items-center gap-3">
              <button type="button" onClick={() => void connectDecisionWallet()} disabled={busy} className="rounded-full border border-teal px-5 py-2.5 text-sm font-bold text-teal disabled:opacity-50">
                {walletAddress ? `Wallet ${shorten(walletAddress, 6)}` : 'Connect decision wallet'}
              </button>
              <button type="button" onClick={() => void prepareAndSend()} disabled={busy || !walletAddress || !assessment} className="rounded-full bg-ink px-5 py-2.5 text-sm font-bold text-white disabled:opacity-50">
                {busy ? 'Working…' : `Prepare and submit ${decision}`}
              </button>
            </div>

            {proposal ? <p className="mt-5 break-all rounded-xl bg-sand p-3 font-mono text-xs text-slate">Decision hash: {proposal.decision_hash}</p> : null}
            {transactionHash ? (
              <p className="mt-3 text-sm text-teal">Broadcast as <a className="font-mono underline" href={`https://sepolia.etherscan.io/tx/${transactionHash}`} target="_blank" rel="noreferrer">{shorten(transactionHash, 10)} ↗</a>. The listener will mark it confirmed after the configured finality depth.</p>
            ) : null}
          </section>
        ) : null}
      </div>
    </div>
  )
}
