import { useState } from 'react'
import { insurerLabel, ipfsUrl, shorten } from '../claim-display.ts'
import type { DisplayReceipt } from '../display-receipt.ts'

function CopyButton({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    await navigator.clipboard.writeText(value)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }

  return (
    <button
      type="button"
      onClick={copy}
      className="rounded-full border border-ink/10 bg-white px-3 py-1.5 text-xs font-semibold text-ink transition hover:border-teal hover:text-teal focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal"
      aria-label={`Copy ${label}`}
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

/**
 * Present one claim across its two timelines: the immediate public anchor and
 * the later PostgreSQL-backed screening result written back to Sepolia.
 */
type ReceiptCardProps = {
  receipt: DisplayReceipt
  assessmentPollingError?: string | null
  onCheckAssessment?: () => void
}

export function ReceiptCard({
  receipt,
  assessmentPollingError = null,
  onCheckAssessment,
}: ReceiptCardProps) {
  const transactionUrl = receipt.transaction_hash
    ? `https://sepolia.etherscan.io/tx/${receipt.transaction_hash}`
    : null
  const assessment = receipt.assessment
  const assessmentUrl = assessment?.transaction_hash
    ? `https://sepolia.etherscan.io/tx/${assessment.transaction_hash}`
    : null
  const probabilityPercent = assessment
    ? (assessment.probability * 100).toFixed(1)
    : null
  const thresholdPercent = assessment
    ? (assessment.threshold * 100).toFixed(0)
    : null

  return (
    <section
      aria-labelledby="receipt-title"
      className="overflow-hidden rounded-3xl border border-teal/20 bg-white shadow-[0_24px_80px_-40px_rgba(14,116,109,0.45)]"
    >
      <div className="flex items-start gap-4 border-b border-ink/8 bg-mint px-6 py-5 sm:px-8">
        <div className="grid size-11 shrink-0 place-items-center rounded-full bg-teal text-xl font-black text-white">
          ✓
        </div>
        <div>
          <p className="text-xs font-bold tracking-[0.18em] text-teal uppercase">
            Sepolia confirmed
          </p>
          <h2 id="receipt-title" className="mt-1 text-2xl font-bold text-ink">
            Claim #{receipt.claim_id} is anchored
          </h2>
          <p className="mt-1 text-sm leading-6 text-slate">
            The IPFS bytes were verified before their hash and pointer were written
            to the registry.
          </p>
        </div>
      </div>

      <dl className="divide-y divide-ink/8 px-6 sm:px-8">
        {receipt.transaction_hash && transactionUrl ? (
          <div className="grid gap-2 py-5 sm:grid-cols-[9rem_1fr_auto] sm:items-center">
            <dt className="text-xs font-bold tracking-[0.14em] text-slate uppercase">
              Transaction
            </dt>
            <dd className="min-w-0 font-mono text-sm text-ink">
              {shorten(receipt.transaction_hash, 12)}
            </dd>
            <div className="flex gap-2">
              <CopyButton
                label="transaction hash"
                value={receipt.transaction_hash}
              />
              <a
                href={transactionUrl}
                target="_blank"
                rel="noreferrer"
                className="rounded-full bg-ink px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-teal focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal"
              >
                Etherscan ↗
              </a>
            </div>
          </div>
        ) : (
          <div className="grid gap-2 py-5 sm:grid-cols-[9rem_1fr] sm:items-center">
            <dt className="text-xs font-bold tracking-[0.14em] text-slate uppercase">
              Sepolia source
            </dt>
            <dd className="text-sm text-ink">
              Read from this claim's current Sepolia contract state
            </dd>
          </div>
        )}

        <div className="grid gap-2 py-5 sm:grid-cols-[9rem_1fr_auto] sm:items-center">
          <dt className="text-xs font-bold tracking-[0.14em] text-slate uppercase">
            IPFS pointer
          </dt>
          <dd className="min-w-0 font-mono text-sm text-ink">
            {shorten(receipt.data_pointer, 12)}
          </dd>
          <div className="flex gap-2">
            <CopyButton label="IPFS pointer" value={receipt.data_pointer} />
            <a
              href={ipfsUrl(receipt.data_pointer)}
              target="_blank"
              rel="noreferrer"
              className="rounded-full border border-ink/10 bg-white px-3 py-1.5 text-xs font-semibold text-ink transition hover:border-teal hover:text-teal focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal"
            >
              Gateway ↗
            </a>
          </div>
        </div>

        <div className="grid gap-2 py-5 sm:grid-cols-[9rem_1fr_auto] sm:items-center">
          <dt className="text-xs font-bold tracking-[0.14em] text-slate uppercase">
            Claim hash
          </dt>
          <dd className="min-w-0 font-mono text-sm text-ink">
            {shorten(receipt.claim_hash, 12)}
          </dd>
          <CopyButton label="claim hash" value={receipt.claim_hash} />
        </div>
      </dl>

      {assessment ? (
        <section className="border-t border-ink/8 bg-sand/55 px-6 py-6 sm:px-8">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-xs font-bold tracking-[0.14em] text-teal uppercase">
              Synthetic fraud screening
            </p>
            <h3 className="mt-1 text-xl font-bold text-ink">
              {assessment.status === 'Flagged'
                ? 'Flagged for human review'
                : 'Queued for human review'}
            </h3>
            <p className="mt-1 text-sm leading-6 text-slate">
              Model {assessment.model_version} · threshold {thresholdPercent}%
            </p>
          </div>
          <div
            className={`rounded-2xl px-5 py-3 text-center ${
              assessment.status === 'Flagged'
                ? 'bg-coral-pale text-coral-dark'
                : 'bg-mint text-teal'
            }`}
          >
            <span className="block text-2xl font-black">{probabilityPercent}%</span>
            <span className="text-xs font-bold uppercase">fraud probability</span>
          </div>
        </div>

        {assessment.duplicate_detection ? (
          <div
            className={`mt-5 rounded-2xl border p-4 ${
              assessment.duplicate_detection.duplicate_detected
                ? 'border-coral/30 bg-coral-pale'
                : 'border-teal/20 bg-mint'
            }`}
          >
            <p className="text-xs font-bold tracking-[0.12em] text-slate uppercase">
              Cross-insurer duplicate screening
            </p>
            <h4 className="mt-1 font-bold text-ink">
              {assessment.duplicate_detection.duplicate_detected
                ? 'Possible duplicate incident found'
                : 'No cross-insurer match found'}
            </h4>
            {assessment.duplicate_detection.duplicate_detected ? (
              <>
                <p className="mt-1 text-sm leading-6 text-slate">
                  This claim from{' '}
                  {insurerLabel(assessment.duplicate_detection.insurer_id)} shares
                  a private incident fingerprint with:
                </p>
                <ul className="mt-2 space-y-1 text-sm font-semibold text-ink">
                  {assessment.duplicate_detection.matches.map((match) => (
                    <li key={`${match.claim_id}:${match.insurer_id}`}>
                      Claim #{match.claim_id} · {insurerLabel(match.insurer_id)}
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-xs leading-5 text-slate">
                  This is a review signal only. Similar synthetic incident details
                  do not prove that either claim is fraudulent.
                </p>
              </>
            ) : (
              <p className="mt-1 text-sm leading-6 text-slate">
                No other participating synthetic insurer has submitted the same
                incident fingerprint.
              </p>
            )}
          </div>
        ) : null}

        <div className="mt-5 grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
          <div>
            <p className="text-xs font-bold tracking-[0.12em] text-slate uppercase">
              Main contributing indicators
            </p>
            <ul className="mt-2 flex flex-wrap gap-2">
              {assessment.reasons.map((reason) => (
                <li
                  key={reason.feature}
                  className="rounded-full border border-ink/10 bg-white px-3 py-1.5 text-xs font-semibold text-ink"
                >
                  {reason.label}
                </li>
              ))}
            </ul>
          </div>
          <div className="text-sm sm:text-right">
            <p className="font-bold text-ink">
              On-chain score: {assessment.fraud_score.toLocaleString()} / 10,000
            </p>
            {assessment.on_chain && assessmentUrl ? (
              <a
                href={assessmentUrl}
                target="_blank"
                rel="noreferrer"
                className="mt-2 inline-flex rounded-full bg-ink px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-teal focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal"
              >
                Assessment transaction ↗
              </a>
            ) : (
              <p className="mt-2 max-w-md text-xs leading-5 text-coral-dark">
                {assessment.error || 'The on-chain assessment is pending.'}
              </p>
            )}
          </div>
        </div>

        <p className="mt-5 border-t border-ink/8 pt-4 text-xs leading-5 text-slate">
          This synthetic research score supports the integration test and must
          not be used to decide a real insurance claim.
        </p>
        </section>
      ) : receipt.chain_state &&
        receipt.chain_state.status !== 'Submitted' ? (
        <section className="border-t border-ink/8 bg-sand/55 px-6 py-6 sm:px-8">
          <p className="text-xs font-bold tracking-[0.14em] text-teal uppercase">
            On-chain screening recorded
          </p>
          <div className="mt-1 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h3 className="text-xl font-bold text-ink">
                Current status:{' '}
                {receipt.chain_state.status === 'UnderReview'
                  ? 'Under review'
                  : receipt.chain_state.status}
              </h3>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-slate">
                The score and status are available from Sepolia. The detailed
                model version, assessment transaction and SHAP indicators are
                not available in the current database for this earlier claim.
              </p>
            </div>
            <div className="shrink-0 rounded-2xl bg-mint px-5 py-3 text-center text-teal">
              <span className="block text-2xl font-black">
                {(receipt.chain_state.fraud_score / 100).toFixed(2)}%
              </span>
              <span className="text-xs font-bold uppercase">on-chain score</span>
            </div>
          </div>
          <p className="mt-5 border-t border-ink/8 pt-4 text-sm font-bold text-ink">
            {receipt.chain_state.fraud_score.toLocaleString()} / 10,000
          </p>
        </section>
      ) : (
        <section className="border-t border-ink/8 bg-sand/55 px-6 py-6 sm:px-8">
          <p className="text-xs font-bold tracking-[0.14em] text-teal uppercase">
            XGBoost screening queued
          </p>
          <h3 className="mt-1 text-xl font-bold text-ink">
            Waiting for the verified claim event
          </h3>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-slate">
            Kafka will pass this claim to the XGBoost worker. This page checks
            PostgreSQL automatically and will show the score and claim-specific
            SHAP reasons as soon as they are ready. You can safely leave this tab
            and return later.
          </p>
          {assessmentPollingError ? (
            <p
              role="alert"
              className="mt-3 max-w-2xl rounded-xl border border-coral/25 bg-coral-pale px-4 py-3 text-sm font-semibold text-coral-dark"
            >
              {assessmentPollingError}
            </p>
          ) : null}
          {onCheckAssessment ? (
            <button
              type="button"
              onClick={onCheckAssessment}
              className="mt-4 rounded-full border border-teal/25 bg-white px-4 py-2 text-sm font-semibold text-teal transition hover:border-teal hover:bg-mint focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal"
            >
              Check again now
            </button>
          ) : null}
        </section>
      )}

      <div className="flex items-center justify-between bg-sand/70 px-6 py-4 text-sm sm:px-8">
        <span className="font-medium text-slate">
          {receipt.block_number === null
            ? 'Current Sepolia contract state'
            : `Block ${receipt.block_number}`}
        </span>
        <span className="font-semibold text-teal">
          {assessment?.on_chain ? 'Lifecycle recorded' : 'Claim anchored'}
        </span>
      </div>
    </section>
  )
}
