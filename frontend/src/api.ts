export type ClaimPayload = {
  insurerId: string
  claimReference: string
  policyReference: string
  claimType: 'collision' | 'theft' | 'fire' | 'flood'
  incidentDate: string
  claimAmountUsd: number
  policyPremiumUsd: number
  vehicleAge: number
  vehicleType: string
  country: string
  regionType: 'urban' | 'rural'
  thirdPartyInjuryFlag: boolean
  totalLossFlag: boolean
  description: string
  evidence: string[]
}

export type AssessmentReason = {
  feature: string
  label: string
  contribution: number
}

export type DuplicateMatch = {
  claim_id: number
  insurer_id: string
}

export type DuplicateDetection = {
  insurer_id: string
  fingerprint_version: string
  duplicate_detected: boolean
  matches: DuplicateMatch[]
}

export type ClaimAssessment = {
  status: 'UnderReview' | 'Flagged'
  fraud_score: number
  probability: number
  threshold: number
  model_version: string
  reasons: AssessmentReason[]
  on_chain: boolean
  transaction_hash: string | null
  block_number: number | null
  error: string | null
  duplicate_detection?: DuplicateDetection | null
}

export type ClaimStatus =
  | 'Submitted'
  | 'UnderReview'
  | 'Approved'
  | 'Rejected'
  | 'Flagged'

export type ClaimSummary = {
  claim_id: number
  claimant: string
  claim_hash: string
  data_pointer: string
  status: ClaimStatus
  fraud_score: number
  submitted_at: number
  updated_at: number
}

export type ClaimPage = {
  items: ClaimSummary[]
  page: number
  page_size: number
  total_items: number
  total_pages: number
}

export type ClaimReceipt = {
  claim_id: number
  transaction_hash: string
  block_number: number
  data_pointer: string
  claim_hash: string
  assessment: ClaimAssessment | null
}

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'
).replace(/\/$/, '')

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

export function isClaimReceipt(value: unknown): value is ClaimReceipt {
  // TypeScript types disappear at runtime. Validate FastAPI responses here so a
  // partial deployment or older backend produces a useful error instead of a
  // broken details card later in the page.
  if (!isRecord(value)) return false

  return (
    typeof value.claim_id === 'number' &&
    typeof value.transaction_hash === 'string' &&
    typeof value.block_number === 'number' &&
    typeof value.data_pointer === 'string' &&
    typeof value.claim_hash === 'string' &&
    (value.assessment === null || isClaimAssessment(value.assessment))
  )
}

function isDuplicateDetection(value: unknown): value is DuplicateDetection {
  if (!isRecord(value) || !Array.isArray(value.matches)) return false

  const matchesAreValid = value.matches.every(
    (match) =>
      isRecord(match) &&
      typeof match.claim_id === 'number' &&
      typeof match.insurer_id === 'string',
  )
  return (
    typeof value.insurer_id === 'string' &&
    typeof value.fingerprint_version === 'string' &&
    typeof value.duplicate_detected === 'boolean' &&
    matchesAreValid &&
    value.duplicate_detected === (value.matches.length > 0)
  )
}

function isClaimAssessment(value: unknown): value is ClaimAssessment {
  if (!isRecord(value)) return false

  return (
    (value.status === 'UnderReview' || value.status === 'Flagged') &&
    typeof value.fraud_score === 'number' &&
    typeof value.probability === 'number' &&
    typeof value.threshold === 'number' &&
    typeof value.model_version === 'string' &&
    Array.isArray(value.reasons) &&
    value.reasons.every(
      (reason) =>
        isRecord(reason) &&
        typeof reason.feature === 'string' &&
        typeof reason.label === 'string' &&
        typeof reason.contribution === 'number',
    ) &&
    typeof value.on_chain === 'boolean' &&
    (typeof value.transaction_hash === 'string' || value.transaction_hash === null) &&
    (typeof value.block_number === 'number' || value.block_number === null) &&
    (typeof value.error === 'string' || value.error === null) &&
    (value.duplicate_detection === undefined ||
      value.duplicate_detection === null ||
      isDuplicateDetection(value.duplicate_detection))
  )
}

function isClaimSummary(value: unknown): value is ClaimSummary {
  if (!isRecord(value)) return false

  const statuses: ClaimStatus[] = [
    'Submitted',
    'UnderReview',
    'Approved',
    'Rejected',
    'Flagged',
  ]

  return (
    typeof value.claim_id === 'number' &&
    typeof value.claimant === 'string' &&
    typeof value.claim_hash === 'string' &&
    typeof value.data_pointer === 'string' &&
    typeof value.status === 'string' &&
    statuses.includes(value.status as ClaimStatus) &&
    typeof value.fraud_score === 'number' &&
    typeof value.submitted_at === 'number' &&
    typeof value.updated_at === 'number'
  )
}

function isClaimPage(value: unknown): value is ClaimPage {
  if (!isRecord(value)) return false

  return (
    Array.isArray(value.items) &&
    value.items.every(isClaimSummary) &&
    typeof value.page === 'number' &&
    typeof value.page_size === 'number' &&
    typeof value.total_items === 'number' &&
    typeof value.total_pages === 'number'
  )
}

function errorMessage(body: unknown, status: number): string {
  if (isRecord(body) && typeof body.detail === 'string') {
    return body.detail
  }

  if (isRecord(body) && Array.isArray(body.detail)) {
    const messages = body.detail
      .filter(isRecord)
      .map((item) => item.msg)
      .filter((message): message is string => typeof message === 'string')
    if (messages.length > 0) return messages.join('. ')
  }

  return `The claims API returned HTTP ${status}`
}

export async function submitClaim(
  payload: ClaimPayload,
  signal?: AbortSignal,
): Promise<ClaimReceipt> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/claims`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error(
      'Could not reach FastAPI. Confirm that the backend is running on port 8000.',
    )
  }

  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new Error(`The claims API returned HTTP ${response.status} without JSON`)
  }

  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isClaimReceipt(body)) {
    throw new Error('The claims API returned an unexpected response shape')
  }

  return body
}

export async function getClaimAssessment(
  claimId: number,
  signal?: AbortSignal,
): Promise<ClaimAssessment | null> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/claims/${claimId}/assessment`, {
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not check the pending model assessment.')
  }

  if (response.status === 404) return null

  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new Error(`The claims API returned HTTP ${response.status} without JSON`)
  }
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isClaimAssessment(body)) {
    throw new Error('The claims API returned an unexpected assessment shape')
  }
  return body
}

export async function listClaims(
  page = 1,
  pageSize = 10,
  signal?: AbortSignal,
): Promise<ClaimPage> {
  const parameters = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/claims?${parameters}`, { signal })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error(
      'Could not load claims. Confirm that FastAPI is running on port 8000.',
    )
  }

  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new Error(`The claims API returned HTTP ${response.status} without JSON`)
  }

  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isClaimPage(body)) {
    throw new Error('The claims API returned an unexpected claims-list shape')
  }

  return body
}
