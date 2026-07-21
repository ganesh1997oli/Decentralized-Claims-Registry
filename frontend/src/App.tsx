import { useMemo, useState, type ChangeEvent, type FormEvent } from 'react'
import { submitClaim, type ClaimPayload, type ClaimReceipt } from './api.ts'

type FormValues = {
  claimReference: string
  policyReference: string
  claimType: string
  incidentDate: string
  amountPounds: string
  description: string
}

const initialForm = (): FormValues => ({
  claimReference: `synthetic-web-${Date.now().toString().slice(-6)}`,
  policyReference: 'synthetic-policy-42',
  claimType: 'vehicle_damage',
  incidentDate: new Date().toISOString().slice(0, 10),
  amountPounds: '2500.00',
  description: 'Synthetic bumper damage submitted through the React form',
})

const IPFS_GATEWAY = (
  import.meta.env.VITE_IPFS_GATEWAY || 'https://gateway.pinata.cloud/ipfs'
).replace(/\/$/, '')

function shorten(value: string, visible = 10): string {
  if (value.length <= visible * 2 + 3) return value
  return `${value.slice(0, visible)}...${value.slice(-visible)}`
}

function ipfsUrl(pointer: string): string {
  return `${IPFS_GATEWAY}/${pointer.replace(/^ipfs:\/\//, '')}`
}

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

function ReceiptCard({ receipt }: { receipt: ClaimReceipt }) {
  const transactionUrl = `https://sepolia.etherscan.io/tx/${receipt.transaction_hash}`

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
        <div className="grid gap-2 py-5 sm:grid-cols-[9rem_1fr_auto] sm:items-center">
          <dt className="text-xs font-bold tracking-[0.14em] text-slate uppercase">
            Transaction
          </dt>
          <dd className="min-w-0 font-mono text-sm text-ink">
            {shorten(receipt.transaction_hash, 12)}
          </dd>
          <div className="flex gap-2">
            <CopyButton label="transaction hash" value={receipt.transaction_hash} />
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

      <div className="flex items-center justify-between bg-sand/70 px-6 py-4 text-sm sm:px-8">
        <span className="font-medium text-slate">Block {receipt.block_number}</span>
        <span className="font-semibold text-teal">Integrity check complete</span>
      </div>
    </section>
  )
}

function App() {
  const [form, setForm] = useState<FormValues>(initialForm)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [receipt, setReceipt] = useState<ClaimReceipt | null>(null)

  const amountPence = useMemo(() => {
    const amount = Number(form.amountPounds)
    return Number.isFinite(amount) ? Math.round(amount * 100) : 0
  }, [form.amountPounds])

  function update(
    event: ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>,
  ) {
    const { name, value } = event.target
    setForm((current) => ({ ...current, [name]: value }))
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setReceipt(null)

    if (amountPence < 1) {
      setError('Enter a claim amount greater than £0.00.')
      return
    }

    const payload: ClaimPayload = {
      claimReference: form.claimReference.trim(),
      policyReference: form.policyReference.trim(),
      claimType: form.claimType,
      incidentDate: form.incidentDate,
      amountPence,
      description: form.description.trim(),
      evidence: [],
    }

    setIsSubmitting(true)
    try {
      setReceipt(await submitClaim(payload))
    } catch (submissionError) {
      setError(
        submissionError instanceof Error
          ? submissionError.message
          : 'The claim could not be submitted.',
      )
    } finally {
      setIsSubmitting(false)
    }
  }

  function resetForm() {
    setForm(initialForm())
    setError(null)
    setReceipt(null)
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-white/10 bg-ink text-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4 sm:px-8 lg:px-12">
          <a
            href="#main"
            className="flex items-center gap-3 rounded-md focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-coral"
          >
            <span className="grid size-10 place-items-center rounded-xl bg-coral font-black text-ink">
              CR
            </span>
            <span>
              <span className="block text-sm font-bold">Claims Registry</span>
              <span className="block text-xs text-white/55">Sepolia prototype</span>
            </span>
          </a>
          <div className="flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3 py-2 text-xs font-semibold text-white/75">
            <span className="size-2 rounded-full bg-emerald-400 shadow-[0_0_0_4px_rgba(52,211,153,0.12)]" />
            Synthetic data only
          </div>
        </div>
      </header>

      <main id="main" className="mx-auto max-w-7xl px-5 py-10 sm:px-8 lg:px-12 lg:py-16">
        <section className="grid items-end gap-8 lg:grid-cols-[1.15fr_0.85fr]">
          <div>
            <p className="mb-5 inline-flex rounded-full border border-teal/15 bg-mint px-3 py-1.5 text-xs font-bold tracking-[0.16em] text-teal uppercase">
              Dissertation milestone M1
            </p>
            <h1 className="max-w-3xl text-4xl leading-[1.05] font-black tracking-[-0.035em] text-ink sm:text-6xl">
              Submit once. Verify everywhere.
            </h1>
            <p className="mt-5 max-w-2xl text-base leading-7 text-slate sm:text-lg">
              This form sends a synthetic claim to FastAPI. The backend pins the
              canonical JSON to IPFS, checks the bytes, then anchors its hash and CID
              on Ethereum Sepolia.
            </p>
          </div>

          <ol className="grid grid-cols-3 gap-2 rounded-2xl border border-ink/8 bg-white p-2 shadow-sm">
            {[
              ['01', 'Validate'],
              ['02', 'Pin to IPFS'],
              ['03', 'Anchor'],
            ].map(([number, label]) => (
              <li key={number} className="rounded-xl bg-sand px-3 py-4 text-center">
                <span className="block text-xs font-black tracking-[0.18em] text-coral-dark">
                  {number}
                </span>
                <span className="mt-1 block text-xs font-semibold text-ink sm:text-sm">
                  {label}
                </span>
              </li>
            ))}
          </ol>
        </section>

        <div className="mt-10 grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_22rem]">
          <section className="rounded-3xl border border-ink/8 bg-white p-6 shadow-[0_24px_80px_-48px_rgba(20,40,51,0.38)] sm:p-8">
            <div className="flex flex-col gap-3 border-b border-ink/8 pb-6 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">
                  Claim intake
                </p>
                <h2 className="mt-1 text-2xl font-bold text-ink">
                  Synthetic motor claim
                </h2>
              </div>
              <button
                type="button"
                onClick={resetForm}
                className="self-start text-sm font-semibold text-slate underline decoration-slate/30 underline-offset-4 transition hover:text-teal"
              >
                Reset sample
              </button>
            </div>

            <form onSubmit={handleSubmit} className="mt-7 space-y-6">
              <div className="grid gap-5 sm:grid-cols-2">
                <label className="field-group">
                  <span className="field-label">Claim reference</span>
                  <input
                    className="field-control"
                    name="claimReference"
                    value={form.claimReference}
                    onChange={update}
                    required
                    minLength={1}
                    maxLength={100}
                    autoComplete="off"
                  />
                </label>

                <label className="field-group">
                  <span className="field-label">Policy reference</span>
                  <input
                    className="field-control"
                    name="policyReference"
                    value={form.policyReference}
                    onChange={update}
                    required
                    minLength={1}
                    maxLength={100}
                    autoComplete="off"
                  />
                </label>

                <label className="field-group">
                  <span className="field-label">Claim type</span>
                  <select
                    className="field-control"
                    name="claimType"
                    value={form.claimType}
                    onChange={update}
                  >
                    <option value="vehicle_damage">Vehicle damage</option>
                    <option value="vehicle_theft">Vehicle theft</option>
                    <option value="collision">Collision</option>
                    <option value="windscreen">Windscreen damage</option>
                    <option value="other_motor">Other motor claim</option>
                  </select>
                </label>

                <label className="field-group">
                  <span className="field-label">Incident date</span>
                  <input
                    className="field-control"
                    type="date"
                    name="incidentDate"
                    value={form.incidentDate}
                    max={new Date().toISOString().slice(0, 10)}
                    onChange={update}
                    required
                  />
                </label>
              </div>

              <label className="field-group max-w-sm">
                <span className="field-label">Claim amount</span>
                <span className="relative block">
                  <span className="pointer-events-none absolute inset-y-0 left-4 flex items-center font-bold text-slate">
                    £
                  </span>
                  <input
                    className="field-control pl-8"
                    type="number"
                    name="amountPounds"
                    value={form.amountPounds}
                    onChange={update}
                    required
                    min="0.01"
                    max="10000000"
                    step="0.01"
                    inputMode="decimal"
                  />
                </span>
                <span className="field-help">Sent as {amountPence.toLocaleString()} pence</span>
              </label>

              <label className="field-group">
                <span className="field-label">Incident description</span>
                <textarea
                  className="field-control min-h-32 resize-y"
                  name="description"
                  value={form.description}
                  onChange={update}
                  required
                  minLength={1}
                  maxLength={2000}
                />
                <span className="field-help">
                  Use fictional information only. Do not enter names, addresses or
                  real policy details.
                </span>
              </label>

              <div className="rounded-2xl border border-coral/25 bg-coral-pale p-4 text-sm leading-6 text-ink">
                <strong className="font-bold">Evidence is intentionally disabled.</strong>{' '}
                This prototype uses public, unencrypted IPFS. Photos and documents
                will be added only after encrypted storage is implemented.
              </div>

              {error && (
                <div
                  role="alert"
                  className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-medium text-red-800"
                >
                  {error}
                </div>
              )}

              <div className="flex flex-col gap-4 border-t border-ink/8 pt-6 sm:flex-row sm:items-center sm:justify-between">
                <p className="max-w-md text-xs leading-5 text-slate">
                  The browser sends this form only to FastAPI. Wallet and Pinata
                  credentials remain server-side.
                </p>
                <button
                  type="submit"
                  disabled={isSubmitting}
                  className="inline-flex min-w-48 items-center justify-center gap-3 rounded-xl bg-coral px-6 py-3.5 text-sm font-black text-ink shadow-[0_10px_28px_-12px_rgba(244,130,98,0.9)] transition hover:-translate-y-0.5 hover:bg-coral-dark hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-coral-dark disabled:cursor-wait disabled:opacity-60 disabled:hover:translate-y-0"
                >
                  {isSubmitting ? (
                    <>
                      <span className="size-4 animate-spin rounded-full border-2 border-ink/25 border-t-ink" />
                      Waiting for Sepolia
                    </>
                  ) : (
                    <>Submit synthetic claim <span aria-hidden="true">→</span></>
                  )}
                </button>
              </div>
            </form>
          </section>

          <aside className="space-y-5 lg:sticky lg:top-6">
            <section className="rounded-3xl bg-ink p-6 text-white">
              <p className="text-xs font-bold tracking-[0.16em] text-coral uppercase">
                Data boundary
              </p>
              <h2 className="mt-2 text-xl font-bold">What goes where?</h2>
              <dl className="mt-5 space-y-5 text-sm">
                <div>
                  <dt className="font-bold text-white">On Sepolia</dt>
                  <dd className="mt-1 leading-6 text-white/60">
                    Claim ID, Keccak-256 hash, IPFS pointer and status.
                  </dd>
                </div>
                <div className="border-t border-white/10 pt-5">
                  <dt className="font-bold text-white">On public IPFS</dt>
                  <dd className="mt-1 leading-6 text-white/60">
                    The synthetic JSON payload. No real personal data.
                  </dd>
                </div>
                <div className="border-t border-white/10 pt-5">
                  <dt className="font-bold text-white">In the browser</dt>
                  <dd className="mt-1 leading-6 text-white/60">
                    Form state and the public transaction receipt only.
                  </dd>
                </div>
              </dl>
            </section>

            <section className="rounded-3xl border border-ink/8 bg-white p-6">
              <p className="text-xs font-bold tracking-[0.16em] text-teal uppercase">
                Network
              </p>
              <div className="mt-3 flex items-center justify-between">
                <span className="font-bold text-ink">Ethereum Sepolia</span>
                <span className="rounded-full bg-mint px-2.5 py-1 text-xs font-bold text-teal">
                  Testnet
                </span>
              </div>
              <p className="mt-3 text-sm leading-6 text-slate">
                Submissions spend test ETH and are publicly visible.
              </p>
            </section>
          </aside>
        </div>

        {receipt && <div className="mt-8"><ReceiptCard receipt={receipt} /></div>}
      </main>

      <footer className="border-t border-ink/8 bg-white/60">
        <div className="mx-auto flex max-w-7xl flex-col gap-2 px-5 py-6 text-xs text-slate sm:flex-row sm:items-center sm:justify-between sm:px-8 lg:px-12">
          <span>Decentralized Claims Registry · Research prototype</span>
          <span>React → FastAPI → IPFS → Sepolia</span>
        </div>
      </footer>
    </div>
  )
}

export default App
