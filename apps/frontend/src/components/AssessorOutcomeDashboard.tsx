import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  getAssessorOutcome,
  getAssessorSession,
  getClaimAssessment,
  listClaims,
  recordAssessorOutcome,
  type AssessorOutcome,
  type AssessorSession,
  type ClaimAssessment,
  type ClaimSummary,
  type HumanFraudOutcome,
} from '../api.ts'
import { ipfsUrl, shorten } from '../claim-display.ts'

const ASSESSOR_KEY_SESSION_STORAGE = 'claims-registry:assessor-outcome-key:v1'

const OUTCOME_OPTIONS: Array<{
  value: HumanFraudOutcome
  label: string
  explanation: string
}> = [
  {
    value: 'ConfirmedFraud',
    label: 'Confirmed fraud',
    explanation: 'Investigation found sufficient evidence of fraud.',
  },
  {
    value: 'Legitimate',
    label: 'Legitimate',
    explanation: 'Investigation found the claim to be non-fraudulent.',
  },
  {
    value: 'Inconclusive',
    label: 'Inconclusive',
    explanation: 'Available evidence does not support a binary conclusion.',
  },
]

function readSessionKey(): string {
  // The assessor key is a short-lived browser credential, not application data.
  // sessionStorage clears with the tab; localStorage and Vite configuration are
  // deliberately avoided because both would retain or expose it more broadly.
  if (typeof window === 'undefined') return ''
  try {
    return window.sessionStorage?.getItem(ASSESSOR_KEY_SESSION_STORAGE) ?? ''
  } catch {
    return ''
  }
}

function storeSessionKey(apiKey: string): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage?.setItem(ASSESSOR_KEY_SESSION_STORAGE, apiKey)
  } catch {
    // Storage is a convenience. The authenticated in-memory session still works.
  }
}

function clearSessionKey(): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage?.removeItem(ASSESSOR_KEY_SESSION_STORAGE)
  } catch {
    // Clearing React state below still removes this tab's active authority.
  }
}

function outcomeLabel(outcome: HumanFraudOutcome): string {
  return OUTCOME_OPTIONS.find((option) => option.value === outcome)?.label ?? outcome
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

/**
 * Dedicated human-review surface.
 *
 * It intentionally does not reuse the insurer submission form or the indexer
 * operations credential. The model screening is evidence presented to a person;
 * this module never turns the probability into a conclusion, updates Sepolia, or
 * starts model training.
 */
export function AssessorOutcomeDashboard() {
  const [apiKey, setApiKey] = useState(() => readSessionKey())
  const [apiKeyDraft, setApiKeyDraft] = useState(() => readSessionKey())
  const [session, setSession] = useState<AssessorSession | null>(null)
  const [claims, setClaims] = useState<ClaimSummary[]>([])
  const [selectedClaim, setSelectedClaim] = useState<ClaimSummary | null>(null)
  const [assessment, setAssessment] = useState<ClaimAssessment | null>(null)
  const [currentOutcome, setCurrentOutcome] = useState<AssessorOutcome | null>(null)
  const [outcomeDraft, setOutcomeDraft] = useState<HumanFraudOutcome | ''>('')
  const [notesDraft, setNotesDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [isSaving, setIsSaving] = useState(false)

  useEffect(() => {
    // A remembered tab credential must still be verified by FastAPI after reload.
    // Failure locks the console; no outcome or assessor identity is trusted from
    // browser storage alone.
    if (!apiKey || session) return
    const controller = new AbortController()
    setIsLoading(true)
    getAssessorSession(apiKey, controller.signal)
      .then((result) => {
        setSession(result)
        setError(null)
      })
      .catch((loadingError: unknown) => {
        if (loadingError instanceof DOMException && loadingError.name === 'AbortError') {
          return
        }
        clearSessionKey()
        setApiKey('')
        setError(
          loadingError instanceof Error
            ? loadingError.message
            : 'The assessor session could not be authenticated.',
        )
      })
      .finally(() => setIsLoading(false))
    return () => controller.abort()
  }, [apiKey, session])

  useEffect(() => {
    // The queue uses the public confirmed index, while all human outcomes remain
    // behind the assessor credential. This keeps ordinary claim pagination free
    // of private investigation fields.
    if (!session) return
    const controller = new AbortController()
    setIsLoading(true)
    listClaims(1, 25, controller.signal)
      .then((page) => {
        setClaims(page.items)
        setSelectedClaim((current) => current ?? page.items[0] ?? null)
        setError(null)
      })
      .catch((loadingError: unknown) => {
        if (loadingError instanceof DOMException && loadingError.name === 'AbortError') {
          return
        }
        setError(
          loadingError instanceof Error
            ? loadingError.message
            : 'The assessor claim queue could not be loaded.',
        )
      })
      .finally(() => setIsLoading(false))
    return () => controller.abort()
  }, [session])

  useEffect(() => {
    if (!selectedClaim || !apiKey || !session) return
    const controller = new AbortController()
    setIsLoading(true)
    setAssessment(null)
    setCurrentOutcome(null)
    setOutcomeDraft('')
    setNotesDraft('')
    Promise.all([
      getClaimAssessment(selectedClaim.claim_id, controller.signal),
      getAssessorOutcome(selectedClaim.claim_id, apiKey, controller.signal),
    ])
      .then(([screening, outcome]) => {
        setAssessment(screening)
        setCurrentOutcome(outcome)
        // Pre-fill a correction from the latest revision, while still requiring
        // an explicit submit action that creates a new immutable revision.
        setOutcomeDraft(outcome?.outcome ?? '')
        setNotesDraft(outcome?.notes ?? '')
        setError(null)
      })
      .catch((loadingError: unknown) => {
        if (loadingError instanceof DOMException && loadingError.name === 'AbortError') {
          return
        }
        setError(
          loadingError instanceof Error
            ? loadingError.message
            : 'The selected claim could not be prepared for human review.',
        )
      })
      .finally(() => setIsLoading(false))
    return () => controller.abort()
  }, [apiKey, selectedClaim, session])

  async function unlock(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const candidate = apiKeyDraft.trim()
    if (!candidate) return
    setIsLoading(true)
    setError(null)
    try {
      const authenticated = await getAssessorSession(candidate)
      storeSessionKey(candidate)
      setApiKey(candidate)
      setSession(authenticated)
    } catch (authenticationError) {
      setError(
        authenticationError instanceof Error
          ? authenticationError.message
          : 'The assessor credential was rejected.',
      )
    } finally {
      setIsLoading(false)
    }
  }

  function lock() {
    clearSessionKey()
    setApiKey('')
    setApiKeyDraft('')
    setSession(null)
    setClaims([])
    setSelectedClaim(null)
    setAssessment(null)
    setCurrentOutcome(null)
    setOutcomeDraft('')
    setNotesDraft('')
    setError(null)
  }

  async function submitOutcome(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selectedClaim || !assessment || !outcomeDraft || !apiKey) return
    setIsSaving(true)
    setError(null)
    try {
      const saved = await recordAssessorOutcome(
        selectedClaim.claim_id,
        {
          outcome: outcomeDraft,
          notes: notesDraft.trim() || null,
        },
        apiKey,
      )
      setCurrentOutcome(saved)
      setOutcomeDraft(saved.outcome)
      setNotesDraft(saved.notes ?? '')
    } catch (savingError) {
      setError(
        savingError instanceof Error
          ? savingError.message
          : 'The human outcome could not be recorded.',
      )
    } finally {
      setIsSaving(false)
    }
  }

  if (!session) {
    return (
      <section className="mx-auto max-w-xl rounded-3xl border border-ink/8 bg-white p-6 shadow-[0_24px_80px_-48px_rgba(20,40,51,0.38)] sm:p-8">
        <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">
          Private human review
        </p>
        <h1 className="mt-2 text-3xl font-black tracking-tight text-ink">
          Assessor outcome console
        </h1>
        <p className="mt-3 text-sm leading-6 text-slate">
          Authenticate with the separate human-assessor key. Insurer, operations,
          wallet and scoring-worker credentials are not accepted here.
        </p>
        <form onSubmit={unlock} className="mt-6">
          <label htmlFor="assessor-api-key" className="text-sm font-bold text-ink">
            Assessor API key
          </label>
          <input
            id="assessor-api-key"
            type="password"
            autoComplete="off"
            value={apiKeyDraft}
            onChange={(event) => setApiKeyDraft(event.target.value)}
            className="mt-2 w-full rounded-xl border border-ink/15 px-4 py-3 font-mono text-sm text-ink outline-none focus:border-teal focus:ring-2 focus:ring-teal/15"
          />
          {error ? <p role="alert" className="mt-3 text-sm text-red-700">{error}</p> : null}
          <button
            type="submit"
            disabled={isLoading || !apiKeyDraft.trim()}
            className="mt-5 rounded-full bg-ink px-5 py-2.5 text-sm font-bold text-white transition hover:bg-teal disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isLoading ? 'Checking…' : 'Unlock human review'}
          </button>
        </form>
      </section>
    )
  }

  return (
    <div className="space-y-6">
      <section className="flex flex-col gap-3 rounded-3xl bg-ink px-6 py-5 text-white sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-xs font-bold tracking-[0.16em] text-coral uppercase">
            Human assessor
          </p>
          <h1 className="mt-1 text-2xl font-black">{session.assessor_reference}</h1>
          <p className="mt-1 text-sm text-white/60">
            Outcomes stay in PostgreSQL and do not change Sepolia status.
          </p>
        </div>
        <button
          type="button"
          onClick={lock}
          className="self-start rounded-full border border-white/15 px-4 py-2 text-xs font-bold text-white/75 hover:bg-white/10 hover:text-white"
        >
          Lock console
        </button>
      </section>

      {error ? (
        <p role="alert" className="rounded-2xl border border-red-200 bg-red-50 px-5 py-3 text-sm font-medium text-red-800">
          {error}
        </p>
      ) : null}

      <div className="grid items-start gap-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
        <section className="overflow-hidden rounded-3xl border border-ink/8 bg-white">
          <div className="border-b border-ink/8 px-5 py-4">
            <p className="text-xs font-bold tracking-[0.14em] text-teal uppercase">Review queue</p>
            <p className="mt-1 text-sm text-slate">Newest 25 confirmed claims</p>
          </div>
          <ul className="divide-y divide-ink/8">
            {claims.map((claim) => (
              <li key={claim.claim_id}>
                <button
                  type="button"
                  onClick={() => setSelectedClaim(claim)}
                  className={`w-full px-5 py-4 text-left transition ${
                    selectedClaim?.claim_id === claim.claim_id
                      ? 'bg-mint'
                      : 'hover:bg-sand/50'
                  }`}
                >
                  <span className="flex items-center justify-between gap-3">
                    <span className="font-bold text-ink">Claim #{claim.claim_id}</span>
                    <span className="text-xs font-bold text-teal">
                      {claim.status === 'UnderReview' ? 'Under review' : claim.status}
                    </span>
                  </span>
                  <span className="mt-1 block font-mono text-xs text-slate">
                    {shorten(claim.claim_hash, 8)} · {(claim.fraud_score / 100).toFixed(2)}%
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {claims.length === 0 && !isLoading ? (
            <p className="px-5 py-8 text-center text-sm text-slate">No indexed claims are available.</p>
          ) : null}
        </section>

        {selectedClaim ? (
          <section className="overflow-hidden rounded-3xl border border-ink/8 bg-white">
            <div className="border-b border-ink/8 bg-sand/60 px-6 py-5 sm:px-8">
              <p className="text-xs font-bold tracking-[0.14em] text-teal uppercase">Evidence and screening</p>
              <div className="mt-1 flex flex-wrap items-center justify-between gap-3">
                <h2 className="text-2xl font-black text-ink">Claim #{selectedClaim.claim_id}</h2>
                <a
                  href={ipfsUrl(selectedClaim.data_pointer)}
                  target="_blank"
                  rel="noreferrer"
                  className="rounded-full border border-ink/10 bg-white px-4 py-2 text-xs font-bold text-teal"
                >
                  Review IPFS evidence ↗
                </a>
              </div>
            </div>

            <div className="px-6 py-6 sm:px-8">
              {isLoading && !assessment ? <p className="text-sm text-slate">Loading review context…</p> : null}
              {!isLoading && !assessment ? (
                <p className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                  Model screening has not completed. A human outcome cannot be recorded yet.
                </p>
              ) : null}

              {assessment ? (
                <>
                  <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-start">
                    <div>
                      <h3 className="text-lg font-bold text-ink">
                        Model screening: {assessment.status === 'Flagged' ? 'Flagged' : 'Under review'}
                      </h3>
                      <p className="mt-1 text-sm leading-6 text-slate">
                        {assessment.model_version} · threshold {(assessment.threshold * 100).toFixed(0)}%
                      </p>
                      <ul className="mt-3 flex flex-wrap gap-2">
                        {assessment.reasons.map((reason) => (
                          <li key={reason.feature} className="rounded-full border border-ink/10 bg-sand px-3 py-1 text-xs font-semibold text-ink">
                            {reason.label}
                          </li>
                        ))}
                      </ul>
                    </div>
                    <div className="rounded-2xl bg-mint px-5 py-3 text-center text-teal">
                      <span className="block text-2xl font-black">{(assessment.probability * 100).toFixed(1)}%</span>
                      <span className="text-xs font-bold uppercase">model probability</span>
                    </div>
                  </div>

                  {currentOutcome ? (
                    <div className="mt-6 rounded-2xl border border-teal/20 bg-mint p-4">
                      <p className="text-xs font-bold tracking-[0.12em] text-teal uppercase">Latest human conclusion</p>
                      <p className="mt-1 text-lg font-black text-ink">{outcomeLabel(currentOutcome.outcome)}</p>
                      <p className="mt-1 text-xs leading-5 text-slate">
                        Revision {currentOutcome.revision} · {currentOutcome.assessor_reference} · {formatDate(currentOutcome.assessed_at)}
                      </p>
                      {currentOutcome.notes ? <p className="mt-3 text-sm leading-6 text-ink">{currentOutcome.notes}</p> : null}
                    </div>
                  ) : null}

                  <form onSubmit={submitOutcome} className="mt-6 border-t border-ink/8 pt-6">
                    <fieldset>
                      <legend className="font-bold text-ink">
                        {currentOutcome ? 'Record a correction revision' : 'Record human fraud outcome'}
                      </legend>
                      <p className="mt-1 text-sm leading-6 text-slate">
                        This is an investigative conclusion, not an Approved or Rejected claim decision.
                      </p>
                      <div className="mt-4 grid gap-3 sm:grid-cols-3">
                        {OUTCOME_OPTIONS.map((option) => (
                          <label key={option.value} className={`cursor-pointer rounded-2xl border p-4 ${outcomeDraft === option.value ? 'border-teal bg-mint' : 'border-ink/10 bg-white'}`}>
                            <input
                              type="radio"
                              name="human-outcome"
                              value={option.value}
                              checked={outcomeDraft === option.value}
                              onChange={() => setOutcomeDraft(option.value)}
                              className="accent-teal"
                            />
                            <span className="ml-2 font-bold text-ink">{option.label}</span>
                            <span className="mt-2 block text-xs leading-5 text-slate">{option.explanation}</span>
                          </label>
                        ))}
                      </div>
                    </fieldset>
                    <label htmlFor="assessor-notes" className="mt-5 block text-sm font-bold text-ink">
                      Review notes <span className="font-normal text-slate">(optional)</span>
                    </label>
                    <textarea
                      id="assessor-notes"
                      rows={4}
                      maxLength={2000}
                      value={notesDraft}
                      onChange={(event) => setNotesDraft(event.target.value)}
                      className="mt-2 w-full rounded-2xl border border-ink/15 px-4 py-3 text-sm leading-6 text-ink outline-none focus:border-teal focus:ring-2 focus:ring-teal/15"
                      placeholder="Record concise evidence or uncertainty; do not copy unnecessary personal data."
                    />
                    <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                      <p className="max-w-xl text-xs leading-5 text-slate">
                        Confirmed fraud and legitimate outcomes may support a future governed dataset. Inconclusive outcomes are excluded. No automatic retraining occurs.
                      </p>
                      <button
                        type="submit"
                        disabled={isSaving || !outcomeDraft}
                        className="rounded-full bg-ink px-5 py-2.5 text-sm font-bold text-white transition hover:bg-teal disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {isSaving ? 'Recording…' : currentOutcome ? 'Save correction' : 'Record outcome'}
                      </button>
                    </div>
                  </form>
                </>
              ) : null}
            </div>
          </section>
        ) : null}
      </div>
    </div>
  )
}

