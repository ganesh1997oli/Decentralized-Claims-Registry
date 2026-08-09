// Claim intake keeps the insurer credential in component memory and emits only
// a server-validated public receipt to the rest of the application.
import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
} from 'react'
import type { ClaimPayload, ClaimReceipt } from '../api.ts'
import { RESEARCH_INSURERS } from '../claim-display.ts'
import {
  GaslessSubmissionTerminalError,
  submitGaslessClaim,
  type SubmissionProgress,
} from '../gasless-submission.ts'

type FormValues = {
  insurerId: string
  claimReference: string
  policyReference: string
  claimType: ClaimPayload['claimType']
  incidentDate: string
  claimAmountUsd: string
  policyPremiumUsd: string
  vehicleAge: string
  vehicleType: string
  country: string
  regionType: ClaimPayload['regionType']
  thirdPartyInjuryFlag: boolean
  totalLossFlag: boolean
  description: string
}

function initialForm(): FormValues {
  // Generate a fresh synthetic reference each time the form is intentionally
  // reset; it is human-readable test data, not the submission's idempotency key.
  return {
    insurerId: RESEARCH_INSURERS[0].id,
    claimReference: `synthetic-web-${Date.now().toString().slice(-6)}`,
    policyReference: 'synthetic-policy-42',
    claimType: 'collision',
    incidentDate: new Date().toISOString().slice(0, 10),
    claimAmountUsd: '2500.00',
    policyPremiumUsd: '480.00',
    vehicleAge: '6',
    vehicleType: 'sedan',
    country: 'Nigeria',
    regionType: 'urban',
    thirdPartyInjuryFlag: false,
    totalLossFlag: false,
    description: 'Synthetic bumper damage submitted through the React form',
  }
}

type ClaimFormProps = {
  onSubmitted: (receipt: ClaimReceipt) => void
}

/**
 * Owns the complete claim-intake workflow.
 *
 * Keeping the credential and validation inside this component gives callers a
 * deliberately narrow interface: they only receive a server-validated receipt.
 */
export function ClaimForm({ onSubmitted }: ClaimFormProps) {
  const [form, setForm] = useState<FormValues>(initialForm)
  // Credentials deliberately live in memory only; never move this state into
  // localStorage, query parameters, analytics, or application logs.
  const [insurerApiKey, setInsurerApiKey] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [progress, setProgress] = useState<SubmissionProgress | null>(null)
  const [error, setError] = useState<string | null>(null)
  const idempotencyKey = useRef<string | null>(null)
  const submissionAbort = useRef<AbortController | null>(null)

  useEffect(
    () => () => {
      // Cancel status polling when the form unmounts. The server-side outbox is
      // durable and continues independently; this only stops obsolete UI work.
      submissionAbort.current?.abort()
    },
    [],
  )

  function update(
    event: ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>,
  ) {
    // Editing claim content invalidates the retry key because the backend binds
    // each Idempotency-Key to a fingerprint of one exact request payload.
    const { name, value } = event.target
    const nextValue =
      event.target instanceof HTMLInputElement && event.target.type === 'checkbox'
        ? event.target.checked
        : value
    idempotencyKey.current = null
    setForm((current) => ({ ...current, [name]: nextValue }))
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    // Perform fast browser validation before any sponsored/IPFS side effect. The
    // API repeats authoritative schema, credential, quota, and role validation.
    event.preventDefault()
    setError(null)

    const claimAmountUsd = Number(form.claimAmountUsd)
    const policyPremiumUsd = Number(form.policyPremiumUsd)
    const vehicleAge = Number(form.vehicleAge)

    if (insurerApiKey.trim().length < 24) {
      setError('Enter the API credential issued for the selected insurer.')
      return
    }
    if (!Number.isFinite(claimAmountUsd) || claimAmountUsd <= 0) {
      setError('Enter a claim amount greater than $0.00.')
      return
    }
    if (!Number.isFinite(policyPremiumUsd) || policyPremiumUsd <= 0) {
      setError('Enter a policy premium greater than $0.00.')
      return
    }
    if (!Number.isInteger(vehicleAge) || vehicleAge < 1 || vehicleAge > 30) {
      setError('Enter a vehicle age between 1 and 30 years.')
      return
    }

    const payload: ClaimPayload = {
      insurerId: form.insurerId,
      claimReference: form.claimReference.trim(),
      policyReference: form.policyReference.trim(),
      claimType: form.claimType,
      incidentDate: form.incidentDate,
      claimAmountUsd,
      policyPremiumUsd,
      vehicleAge,
      vehicleType: form.vehicleType,
      country: form.country,
      regionType: form.regionType,
      thirdPartyInjuryFlag: form.thirdPartyInjuryFlag,
      totalLossFlag: form.totalLossFlag,
      description: form.description.trim(),
      evidence: [],
    }

    setIsSubmitting(true)
    setProgress('Connecting wallet')
    const controller = new AbortController()
    submissionAbort.current = controller
    try {
      // Reuse the key after recoverable transport errors so FastAPI returns the
      // same durable workflow. Rotate it only after success, a terminal state,
      // form edits, or an explicit reset.
      idempotencyKey.current ??= crypto.randomUUID()
      const receipt = await submitGaslessClaim({
        claim: payload,
        insurerApiKey: insurerApiKey.trim(),
        idempotencyKey: idempotencyKey.current,
        signal: controller.signal,
        onProgress: setProgress,
      })
      idempotencyKey.current = null
      onSubmitted(receipt)
    } catch (submissionError) {
      if (submissionError instanceof GaslessSubmissionTerminalError) {
        idempotencyKey.current = null
      }
      setError(
        submissionError instanceof Error
          ? submissionError.message
          : 'The claim could not be submitted.',
      )
    } finally {
      submissionAbort.current = null
      setIsSubmitting(false)
      setProgress(null)
    }
  }

  function resetForm() {
    // Reset secrets and retry identity together; neither should leak into the
    // next synthetic claim prepared from this browser tab.
    setForm(initialForm())
    setInsurerApiKey('')
    setProgress(null)
    setError(null)
    idempotencyKey.current = null
  }

  return (
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
                disabled={isSubmitting}
                className="self-start text-sm font-semibold text-slate underline decoration-slate/30 underline-offset-4 transition hover:text-teal disabled:cursor-not-allowed disabled:opacity-50"
              >
                Reset sample
              </button>
            </div>

            <form onSubmit={handleSubmit} className="mt-7">
              <fieldset disabled={isSubmitting} className="space-y-6">
              <div className="grid gap-5 sm:grid-cols-2">
                <div className="field-group sm:col-span-2">
                  <label className="field-label" htmlFor="insurer-id">
                    Synthetic insurer
                  </label>
                  <select
                    id="insurer-id"
                    className="field-control"
                    name="insurerId"
                    value={form.insurerId}
                    onChange={update}
                    aria-describedby="insurer-id-help"
                  >
                    {RESEARCH_INSURERS.map((insurer) => (
                      <option key={insurer.id} value={insurer.id}>
                        {insurer.label}
                      </option>
                    ))}
                  </select>
                  <span className="field-help" id="insurer-id-help">
                    The API credential must belong to this insurer. FastAPI rejects
                    a user-selected label that does not match the authenticated
                    principal.
                  </span>
                </div>

                <div className="field-group sm:col-span-2">
                  <label className="field-label" htmlFor="insurer-api-key">
                    Insurer API credential
                  </label>
                  <input
                    id="insurer-api-key"
                    className="field-control font-mono"
                    type="password"
                    value={insurerApiKey}
                    onChange={(event) => {
                      idempotencyKey.current = null
                      setInsurerApiKey(event.target.value)
                    }}
                    required
                    minLength={24}
                    maxLength={256}
                    autoComplete="off"
                    spellCheck={false}
                    aria-describedby="insurer-api-key-help"
                  />
                  <span className="field-help" id="insurer-api-key-help">
                    Enter the synthetic insurer credential at runtime. It is sent
                    only in the request header and is never saved in browser storage.
                  </span>
                </div>

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
                    <option value="collision">Collision</option>
                    <option value="theft">Theft</option>
                    <option value="fire">Fire</option>
                    <option value="flood">Flood</option>
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

              <div className="grid gap-5 sm:grid-cols-2">
                <label className="field-group">
                  <span className="field-label">Claim amount (USD)</span>
                  <input
                    className="field-control"
                    type="number"
                    name="claimAmountUsd"
                    value={form.claimAmountUsd}
                    onChange={update}
                    required
                    min="0.01"
                    max="100000000"
                    step="0.01"
                    inputMode="decimal"
                  />
                </label>

                <label className="field-group">
                  <span className="field-label">Annual policy premium (USD)</span>
                  <input
                    className="field-control"
                    type="number"
                    name="policyPremiumUsd"
                    value={form.policyPremiumUsd}
                    onChange={update}
                    required
                    min="0.01"
                    max="10000000"
                    step="0.01"
                    inputMode="decimal"
                  />
                </label>

                <label className="field-group">
                  <span className="field-label">Vehicle age</span>
                  <input
                    className="field-control"
                    type="number"
                    name="vehicleAge"
                    value={form.vehicleAge}
                    onChange={update}
                    required
                    min="1"
                    max="30"
                    step="1"
                  />
                </label>

                <label className="field-group">
                  <span className="field-label">Vehicle type</span>
                  <select
                    className="field-control"
                    name="vehicleType"
                    value={form.vehicleType}
                    onChange={update}
                  >
                    {[
                      'sedan',
                      'suv',
                      'pickup',
                      'minibus',
                      'truck',
                      'motorcycle',
                      'bus',
                      'hatchback',
                      'van',
                      'other',
                    ].map((vehicleType) => (
                      <option key={vehicleType} value={vehicleType}>
                        {vehicleType}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="field-group">
                  <span className="field-label">Country</span>
                  <select
                    className="field-control"
                    name="country"
                    value={form.country}
                    onChange={update}
                  >
                    {[
                      'South Africa',
                      'Nigeria',
                      'Kenya',
                      'Ghana',
                      'Tanzania',
                      'Uganda',
                      'Rwanda',
                      'Ethiopia',
                      'Senegal',
                      "Cote d'Ivoire",
                      'Zambia',
                      'Mozambique',
                    ].map((country) => (
                      <option key={country} value={country}>
                        {country}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="field-group">
                  <span className="field-label">Region type</span>
                  <select
                    className="field-control"
                    name="regionType"
                    value={form.regionType}
                    onChange={update}
                  >
                    <option value="urban">Urban</option>
                    <option value="rural">Rural</option>
                  </select>
                </label>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <label className="flex items-center gap-3 rounded-2xl border border-ink/10 bg-sand/45 px-4 py-3 text-sm font-semibold text-ink">
                  <input
                    type="checkbox"
                    name="thirdPartyInjuryFlag"
                    checked={form.thirdPartyInjuryFlag}
                    onChange={update}
                    className="size-4 accent-teal"
                  />
                  Third-party injury involved
                </label>
                <label className="flex items-center gap-3 rounded-2xl border border-ink/10 bg-sand/45 px-4 py-3 text-sm font-semibold text-ink">
                  <input
                    type="checkbox"
                    name="totalLossFlag"
                    checked={form.totalLossFlag}
                    onChange={update}
                    className="size-4 accent-teal"
                  />
                  Vehicle is a total loss
                </label>
              </div>

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
                  Your insurer wallet signs the exact claim request; it never pays gas.
                  A restricted relayer sponsors only that verified contract call.
                </p>
                <button
                  type="submit"
                  disabled={isSubmitting}
                  className="inline-flex min-w-48 items-center justify-center gap-3 rounded-xl bg-coral px-6 py-3.5 text-sm font-black text-ink shadow-[0_10px_28px_-12px_rgba(244,130,98,0.9)] transition hover:-translate-y-0.5 hover:bg-coral-dark hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-coral-dark disabled:cursor-wait disabled:opacity-60 disabled:hover:translate-y-0"
                >
                  {isSubmitting ? (
                    <>
                      <span className="size-4 animate-spin rounded-full border-2 border-ink/25 border-t-ink" />
                      {progress ?? 'Submitting sponsored transaction'}
                    </>
                  ) : (
                    <>Sign & submit gaslessly <span aria-hidden="true">→</span></>
                  )}
                </button>
              </div>
              </fieldset>
            </form>
          </section>
  )
}
