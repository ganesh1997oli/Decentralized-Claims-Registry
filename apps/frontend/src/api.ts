// Typed boundary between the browser and FastAPI. TypeScript disappears in the
// built application, so every untrusted JSON response is checked again before
// React is allowed to render or persist it.
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
  indexed_through_block: number | null
}

export type IndexerState =
  | 'healthy'
  | 'catching_up'
  | 'stalled'
  | 'uninitialized'
  | 'degraded'

export type ClaimStatusCounts = {
  submitted: number
  under_review: number
  approved: number
  rejected: number
  flagged: number
}

export type ClaimIndexEvent = {
  event_id: string
  claim_id: number
  event_type: 'ClaimSubmitted' | 'ClaimAssessed'
  block_number: number
  transaction_hash: string
  log_index: number
  event_timestamp: number
  status: string
  fraud_score: number
  indexed_at: string
}

export type IndexerEventSearch = {
  claimId: number | null
  transactionHash: string | null
  eventType: ClaimIndexEvent['event_type'] | null
  status: ClaimStatus | null
  fromBlock: number | null
  toBlock: number | null
  limit: number
}

export type ClaimIndexEventPage = {
  items: ClaimIndexEvent[]
  page_size: number
  next_cursor: string | null
}

export type ClaimIndexReconciliation = {
  indexed_through_block: number
  chain_claims: number
  indexed_claims: number
  missing_claim_ids: number[]
  unexpected_claim_ids: number[]
  mismatched_claim_ids: number[]
  consistent: boolean
  duration_ms: number
  checked_at: string
}

export type IndexerOperations = {
  state: IndexerState
  rpc_status: 'available' | 'unavailable'
  deployment_id: string
  chain_id: number
  contract_address: string
  confirmation_blocks: number
  stale_after_seconds: number
  latest_block: number | null
  safe_block: number | null
  indexed_through_block: number | null
  block_lag: number | null
  checkpoint_updated_at: string | null
  checkpoint_age_seconds: number | null
  total_claims: number
  total_events: number
  submitted_events: number
  assessed_events: number
  claim_status_counts: ClaimStatusCounts
  recent_events: ClaimIndexEvent[]
  last_reconciliation: ClaimIndexReconciliation | null
  observed_at: string
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
  // JSON values must be narrowed to a non-null object before property access;
  // arrays are rejected later by shape-specific validators where relevant.
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
  // Enforce the backend invariant that the boolean summary agrees with whether
  // concrete matches exist, avoiding a contradictory review message in the UI.
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
  // Assessment polling crosses independently deployed API/worker versions. Check
  // every field before merging a result into a persisted browser receipt.
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
  // A claim summary is public projected state. Restrict status to the known domain
  // vocabulary while validating every value rendered in the dashboard table.
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
  // Validate pagination metadata together with all rows so the workspace never
  // navigates using totals from an incompatible or partial response.
  if (!isRecord(value)) return false

  return (
    Array.isArray(value.items) &&
    value.items.every(isClaimSummary) &&
    typeof value.page === 'number' &&
    typeof value.page_size === 'number' &&
    typeof value.total_items === 'number' &&
    typeof value.total_pages === 'number' &&
    (typeof value.indexed_through_block === 'number' ||
      value.indexed_through_block === null)
  )
}

function isNullableNumber(value: unknown): value is number | null {
  // RPC/checkpoint fields use null as an explicit unavailable state, not zero.
  return typeof value === 'number' || value === null
}

function isNullableTimestamp(value: unknown): value is string | null {
  // Timestamps remain ISO strings until locale formatting at the display boundary.
  return typeof value === 'string' || value === null
}

function isNumberArray(value: unknown): value is number[] {
  // Reconciliation ID lists must be homogeneous before their counts are trusted.
  return Array.isArray(value) && value.every((item) => typeof item === 'number')
}

function isClaimStatusCounts(value: unknown): value is ClaimStatusCounts {
  // Validate all five Solidity enum buckets as a unit before drawing percentages.
  if (!isRecord(value)) return false
  return [
    value.submitted,
    value.under_review,
    value.approved,
    value.rejected,
    value.flagged,
  ].every((count) => typeof count === 'number')
}

function isClaimIndexEvent(value: unknown): value is ClaimIndexEvent {
  // This validator is the runtime trust boundary for an individual audit row.
  // It intentionally accepts the backend's status string so a future unknown
  // Solidity enum remains visible instead of invalidating the entire response.
  if (!isRecord(value)) return false
  return (
    typeof value.event_id === 'string' &&
    typeof value.claim_id === 'number' &&
    (value.event_type === 'ClaimSubmitted' ||
      value.event_type === 'ClaimAssessed') &&
    typeof value.block_number === 'number' &&
    typeof value.transaction_hash === 'string' &&
    typeof value.log_index === 'number' &&
    typeof value.event_timestamp === 'number' &&
    typeof value.status === 'string' &&
    typeof value.fraud_score === 'number' &&
    typeof value.indexed_at === 'string'
  )
}

function isClaimIndexEventPage(value: unknown): value is ClaimIndexEventPage {
  // Validate both the bounded items and the opaque continuation token. The
  // browser never decodes the token; it only returns it to the same API.
  if (!isRecord(value)) return false
  return (
    Array.isArray(value.items) &&
    value.items.every(isClaimIndexEvent) &&
    typeof value.page_size === 'number' &&
    (value.next_cursor === null || typeof value.next_cursor === 'string')
  )
}

function isClaimIndexReconciliation(
  value: unknown,
): value is ClaimIndexReconciliation {
  // Reconciliation is durable audit evidence, so reject partial deployments
  // instead of rendering missing difference arrays as a false success.
  if (!isRecord(value)) return false
  return (
    typeof value.indexed_through_block === 'number' &&
    typeof value.chain_claims === 'number' &&
    typeof value.indexed_claims === 'number' &&
    isNumberArray(value.missing_claim_ids) &&
    isNumberArray(value.unexpected_claim_ids) &&
    isNumberArray(value.mismatched_claim_ids) &&
    typeof value.consistent === 'boolean' &&
    typeof value.duration_ms === 'number' &&
    typeof value.checked_at === 'string'
  )
}

function isIndexerOperations(value: unknown): value is IndexerOperations {
  // TypeScript types are erased in production. This full structural check keeps
  // an older/incompatible backend from being interpreted as healthy telemetry.
  // Nullable RPC-derived fields are valid because PostgreSQL data survives an
  // RPC outage and the backend deliberately returns a degraded snapshot.
  if (!isRecord(value)) return false
  const states: IndexerState[] = [
    'healthy',
    'catching_up',
    'stalled',
    'uninitialized',
    'degraded',
  ]
  return (
    typeof value.state === 'string' &&
    states.includes(value.state as IndexerState) &&
    (value.rpc_status === 'available' || value.rpc_status === 'unavailable') &&
    typeof value.deployment_id === 'string' &&
    typeof value.chain_id === 'number' &&
    typeof value.contract_address === 'string' &&
    typeof value.confirmation_blocks === 'number' &&
    typeof value.stale_after_seconds === 'number' &&
    isNullableNumber(value.latest_block) &&
    isNullableNumber(value.safe_block) &&
    isNullableNumber(value.indexed_through_block) &&
    isNullableNumber(value.block_lag) &&
    isNullableTimestamp(value.checkpoint_updated_at) &&
    isNullableNumber(value.checkpoint_age_seconds) &&
    typeof value.total_claims === 'number' &&
    typeof value.total_events === 'number' &&
    typeof value.submitted_events === 'number' &&
    typeof value.assessed_events === 'number' &&
    isClaimStatusCounts(value.claim_status_counts) &&
    Array.isArray(value.recent_events) &&
    value.recent_events.every(isClaimIndexEvent) &&
    (value.last_reconciliation === null ||
      isClaimIndexReconciliation(value.last_reconciliation)) &&
    typeof value.observed_at === 'string'
  )
}

function errorMessage(body: unknown, status: number): string {
  // FastAPI can return a plain detail or a validation-error array. Normalize both
  // without exposing arbitrary response objects through React error rendering.
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
  insurerApiKey: string,
  signal?: AbortSignal,
): Promise<ClaimReceipt> {
  // The insurer key is header-only and the canonical claim is JSON request data.
  // Network, non-JSON, HTTP, and response-shape failures are separated so the form
  // can present an actionable message without trusting unvalidated server output.
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/claims`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Insurer-API-Key': insurerApiKey,
      },
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
  // A 404 is the normal asynchronous "not scored yet" state and maps to null.
  // Other failures remain errors so the adaptive polling loop can disclose them
  // while retaining the last known receipt.
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
  // Page controls are encoded as query parameters and the whole response is
  // runtime-validated before it can replace workspace pagination state.
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

export async function getIndexerOperations(
  operationsApiKey: string,
  signal?: AbortSignal,
): Promise<IndexerOperations> {
  // The raw operator key travels only in a request header: never in the URL,
  // query logs, or Vite build configuration. AbortError is preserved so React
  // can discard superseded refreshes without showing a network-failure banner.
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/operations/indexer`, {
      headers: { 'X-Operations-API-Key': operationsApiKey },
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error(
      'Could not reach indexer operations. Confirm that FastAPI is running.',
    )
  }

  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new Error(`The claims API returned HTTP ${response.status} without JSON`)
  }
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isIndexerOperations(body)) {
    throw new Error('The claims API returned an unexpected operations response')
  }
  return body
}

export async function searchIndexerEvents(
  operationsApiKey: string,
  filters: IndexerEventSearch,
  cursor: string | null = null,
  signal?: AbortSignal,
): Promise<ClaimIndexEventPage> {
  // Search parameters contain only public chain metadata. The opaque keyset
  // cursor is forwarded without interpretation, while the operations key remains
  // isolated in its header. The backend repeats all validation before SQL.
  const parameters = new URLSearchParams({ limit: String(filters.limit) })
  if (filters.claimId !== null) {
    parameters.set('claim_id', String(filters.claimId))
  }
  if (filters.transactionHash !== null) {
    parameters.set('transaction_hash', filters.transactionHash)
  }
  if (filters.eventType !== null) {
    parameters.set('event_type', filters.eventType)
  }
  if (filters.status !== null) parameters.set('status', filters.status)
  if (filters.fromBlock !== null) {
    parameters.set('from_block', String(filters.fromBlock))
  }
  if (filters.toBlock !== null) {
    parameters.set('to_block', String(filters.toBlock))
  }
  if (cursor !== null) parameters.set('cursor', cursor)

  let response: Response
  try {
    response = await fetch(
      `${API_BASE_URL}/operations/indexer/events?${parameters}`,
      {
        headers: { 'X-Operations-API-Key': operationsApiKey },
        signal,
      },
    )
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error(
      'Could not search indexer events. Confirm that FastAPI is running.',
    )
  }

  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new Error(`The claims API returned HTTP ${response.status} without JSON`)
  }
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isClaimIndexEventPage(body)) {
    throw new Error('The claims API returned an unexpected event-search response')
  }
  return body
}
