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

export type ClaimantChallenge = {
  challenge_id: string
  message: string
  expires_at: string
}

export type ClaimantSession = {
  access_token: string
  token_type: 'bearer'
  expires_at: string
  claimant_address: string
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

// Human investigative conclusions remain separate from both the model's
// UnderReview/Flagged result and the contract's Approved/Rejected lifecycle.
export type HumanFraudOutcome =
  | 'ConfirmedFraud'
  | 'Legitimate'
  | 'Inconclusive'

export type AssessorSession = {
  assessor_reference: string
}

export type AssessorOutcome = {
  outcome_id: string
  claim_id: number
  revision: number
  outcome: HumanFraudOutcome
  assessor_reference: string
  notes: string | null
  assessed_at: string
}

export type AssessorOutcomeInput = {
  outcome: HumanFraudOutcome
  notes: string | null
}

export type GovernanceSession = {
  governance_reference: string
  insurer_address: string
}

export type CoverageDecisionStatus = 'Approved' | 'Rejected'

export type CoverageDecisionProposal = {
  decision_id: string
  claim_id: number
  decision_status: CoverageDecisionStatus
  decision_hash: string
  decision_maker_address: string
  proposed_by: string
  human_outcome_id: string
  human_outcome_revision: number
  created_at: string
  confirmed_transaction_hash: string | null
  confirmed_at: string | null
  chain_id: number
  contract_address: string
  transaction_data: string
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
  event_type: 'ClaimSubmitted' | 'ClaimAssessed' | 'ClaimDecided'
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

export type EIP712TypedData = {
  types: Record<string, { name: string; type: string }[]>
  primaryType: 'ForwardRequest'
  domain: {
    name: 'ClaimsRegistryForwarder'
    version: '1'
    chainId: number
    verifyingContract: string
  }
  message: {
    from: string
    to: string
    value: string
    gas: string
    nonce: string
    deadline: string
    data: string
  }
}

export type GaslessSubmissionState =
  | 'preparing'
  | 'prepared'
  | 'authorized'
  | 'signed'
  | 'broadcast'
  | 'confirmed'
  | 'failed'
  | 'expired'

export type GaslessSubmission = {
  submission_id: string
  state: GaslessSubmissionState
  signer_address: string
  chain_id: number
  contract_address: string
  forwarder_address: string
  claim_hash: string | null
  data_pointer: string | null
  deadline: number | null
  typed_data: EIP712TypedData | null
  receipt: ClaimReceipt | null
  error_code: string | null
  poll_after_ms: number
}

export type GaslessNetwork = {
  chain_id: number
  contract_address: string
  forwarder_address: string
  domain_name: 'ClaimsRegistryForwarder'
  domain_version: '1'
}

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'
).replace(/\/$/, '')

function isRecord(value: unknown): value is Record<string, unknown> {
  // JSON values must be narrowed to a non-null object before property access;
  // arrays are rejected later by shape-specific validators where relevant.
  return typeof value === 'object' && value !== null
}

function isAddress(value: unknown): value is string {
  return typeof value === 'string' && /^0x[0-9a-fA-F]{40}$/.test(value)
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value))
}

function isClaimantChallenge(value: unknown): value is ClaimantChallenge {
  return (
    isRecord(value) &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      String(value.challenge_id),
    ) &&
    typeof value.message === 'string' &&
    value.message.length > 0 &&
    isTimestamp(value.expires_at)
  )
}

function isClaimantSession(value: unknown): value is ClaimantSession {
  return (
    isRecord(value) &&
    typeof value.access_token === 'string' &&
    value.access_token.length >= 32 &&
    value.token_type === 'bearer' &&
    isTimestamp(value.expires_at) &&
    isAddress(value.claimant_address)
  )
}

/** Validates the public chain receipt before it enters React application state. */
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

function isEip712TypedData(value: unknown): value is EIP712TypedData {
  // Use an exact allowlist for the wallet prompt. Accepting extra types or a
  // different primary type could authorize semantics the UI never reviewed.
  if (!isRecord(value) || !isRecord(value.domain) || !isRecord(value.message)) {
    return false
  }
  if (!isRecord(value.types)) return false
  const expectedTypes = {
    EIP712Domain: [
      { name: 'name', type: 'string' },
      { name: 'version', type: 'string' },
      { name: 'chainId', type: 'uint256' },
      { name: 'verifyingContract', type: 'address' },
    ],
    ForwardRequest: [
      { name: 'from', type: 'address' },
      { name: 'to', type: 'address' },
      { name: 'value', type: 'uint256' },
      { name: 'gas', type: 'uint256' },
      { name: 'nonce', type: 'uint256' },
      { name: 'deadline', type: 'uint48' },
      { name: 'data', type: 'bytes' },
    ],
  }
  const typesAreExact =
    Object.keys(value.types).length === 2 &&
    JSON.stringify(value.types.EIP712Domain) ===
      JSON.stringify(expectedTypes.EIP712Domain) &&
    JSON.stringify(value.types.ForwardRequest) ===
      JSON.stringify(expectedTypes.ForwardRequest)
  const isDecimal = (field: unknown) =>
    // uint256 values arrive as decimal strings to avoid JavaScript precision
    // loss; the EIP-712 wallet encoder accepts these lossless representations.
    typeof field === 'string' && /^[0-9]+$/.test(field)
  return (
    typesAreExact &&
    value.primaryType === 'ForwardRequest' &&
    value.domain.name === 'ClaimsRegistryForwarder' &&
    value.domain.version === '1' &&
    typeof value.domain.chainId === 'number' &&
    isAddress(value.domain.verifyingContract) &&
    isAddress(value.message.from) &&
    isAddress(value.message.to) &&
    isDecimal(value.message.value) &&
    isDecimal(value.message.gas) &&
    isDecimal(value.message.nonce) &&
    isDecimal(value.message.deadline) &&
    typeof value.message.data === 'string' &&
    /^0x(?:[0-9a-fA-F]{2})+$/.test(value.message.data)
  )
}

function isGaslessSubmission(value: unknown): value is GaslessSubmission {
  // Validate both field shapes and state-specific invariants. In particular, a
  // prepared response must be signable and a confirmed response must contain a
  // complete public receipt before orchestration code can advance.
  if (!isRecord(value)) return false
  const states: GaslessSubmissionState[] = [
    'preparing',
    'prepared',
    'authorized',
    'signed',
    'broadcast',
    'confirmed',
    'failed',
    'expired',
  ]
  return (
    typeof value.submission_id === 'string' &&
    typeof value.state === 'string' &&
    states.includes(value.state as GaslessSubmissionState) &&
    isAddress(value.signer_address) &&
    typeof value.chain_id === 'number' &&
    isAddress(value.contract_address) &&
    isAddress(value.forwarder_address) &&
    (value.claim_hash === null ||
      (typeof value.claim_hash === 'string' &&
        /^0x[0-9a-fA-F]{64}$/.test(value.claim_hash))) &&
    (value.data_pointer === null ||
      (typeof value.data_pointer === 'string' &&
        /^ipfs:\/\/[A-Za-z0-9]{1,121}$/.test(value.data_pointer))) &&
    (value.deadline === null || typeof value.deadline === 'number') &&
    (value.typed_data === null || isEip712TypedData(value.typed_data)) &&
    (value.receipt === null || isClaimReceipt(value.receipt)) &&
    (value.error_code === null || typeof value.error_code === 'string') &&
    typeof value.poll_after_ms === 'number' &&
    (value.state !== 'prepared' || value.typed_data !== null) &&
    (value.state !== 'confirmed' || value.receipt !== null)
  )
}

function isGaslessNetwork(value: unknown): value is GaslessNetwork {
  // Pin the EIP-712 domain name/version used by the deployed forwarder; an API
  // returning a different signing protocol is incompatible, not a soft upgrade.
  return (
    isRecord(value) &&
    typeof value.chain_id === 'number' &&
    isAddress(value.contract_address) &&
    isAddress(value.forwarder_address) &&
    value.domain_name === 'ClaimsRegistryForwarder' &&
    value.domain_version === '1'
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

function isHumanFraudOutcome(value: unknown): value is HumanFraudOutcome {
  // Keep the browser vocabulary exactly aligned with the database constraint.
  // Approved/Rejected must never become accepted aliases at this trust boundary.
  return (
    value === 'ConfirmedFraud' ||
    value === 'Legitimate' ||
    value === 'Inconclusive'
  )
}

function isAssessorSession(value: unknown): value is AssessorSession {
  return isRecord(value) && typeof value.assessor_reference === 'string'
}

function isAssessorOutcome(value: unknown): value is AssessorOutcome {
  // This authenticated response can later inform research-label governance, so
  // reject any partial or contradictory shape instead of rendering defaults.
  if (!isRecord(value)) return false
  return (
    typeof value.outcome_id === 'string' &&
    typeof value.claim_id === 'number' &&
    typeof value.revision === 'number' &&
    isHumanFraudOutcome(value.outcome) &&
    typeof value.assessor_reference === 'string' &&
    (value.notes === null || typeof value.notes === 'string') &&
    typeof value.assessed_at === 'string'
  )
}

function isGovernanceSession(value: unknown): value is GovernanceSession {
  return (
    isRecord(value) &&
    typeof value.governance_reference === 'string' &&
    isAddress(value.insurer_address)
  )
}

function isCoverageDecisionProposal(
  value: unknown,
): value is CoverageDecisionProposal {
  if (!isRecord(value)) return false
  return (
    typeof value.decision_id === 'string' &&
    typeof value.claim_id === 'number' &&
    (value.decision_status === 'Approved' || value.decision_status === 'Rejected') &&
    typeof value.decision_hash === 'string' &&
    isAddress(value.decision_maker_address) &&
    typeof value.proposed_by === 'string' &&
    typeof value.human_outcome_id === 'string' &&
    typeof value.human_outcome_revision === 'number' &&
    isTimestamp(value.created_at) &&
    (value.confirmed_transaction_hash === null ||
      typeof value.confirmed_transaction_hash === 'string') &&
    (value.confirmed_at === null || isTimestamp(value.confirmed_at)) &&
    typeof value.chain_id === 'number' &&
    isAddress(value.contract_address) &&
    typeof value.transaction_data === 'string' &&
    /^0x[0-9a-fA-F]+$/.test(value.transaction_data)
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
      value.event_type === 'ClaimAssessed' ||
      value.event_type === 'ClaimDecided') &&
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

/** Requests the exact, short-lived sign-in message for a claimant wallet. */
export async function createClaimantChallenge(
  walletAddress: string,
  signal?: AbortSignal,
): Promise<ClaimantChallenge> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/claimant/session/challenge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ walletAddress }),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not start claimant wallet verification.')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isClaimantChallenge(body)) {
    throw new Error('The claims API returned an unexpected wallet challenge')
  }
  return body
}

/** Exchanges a one-time wallet proof for an in-memory claimant bearer session. */
export async function createClaimantSession(
  challengeId: string,
  signature: string,
  signal?: AbortSignal,
): Promise<ClaimantSession> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/claimant/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ challenge_id: challengeId, signature }),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not complete claimant wallet verification.')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isClaimantSession(body)) {
    throw new Error('The claims API returned an unexpected claimant session')
  }
  return body
}

/**
 * Fetches the deployment identity used to configure the wallet signing flow.
 *
 * This endpoint is public because it contains only chain metadata. The caller
 * must still compare it with the authenticated preparation response before
 * allowing a signature, which protects against configuration changing mid-flow.
 */
export async function getGaslessNetwork(
  signal?: AbortSignal,
): Promise<GaslessNetwork> {
  // Deployment discovery is intentionally unauthenticated and read-only. The
  // browser compares these values with the subsequent prepared response before
  // it allows the verified claimant submitter wallet to sign.
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/claims/gasless/config`, { signal })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error(
      'Could not reach FastAPI. Confirm that the backend is running on port 8000.',
    )
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isGaslessNetwork(body)) {
    throw new Error('The claims API returned an unexpected gasless configuration')
  }
  return body
}

/**
 * Creates or resumes the credential-scoped durable preparation for one claim.
 *
 * The idempotency key represents this exact claim attempt. Reusing it after an
 * uncertain HTTP result is safe; reusing it with changed claim data is rejected
 * by the backend rather than silently creating a different authorization.
 */
export async function prepareGaslessClaim(
  payload: ClaimPayload,
  accessToken: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<GaslessSubmission> {
  // The bearer token proves wallet ownership. Policy eligibility, claimant
  // identity, insurer selection, and the on-chain submitter are resolved by the
  // backend instead of trusting extra identity headers from the browser.
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/claims/gasless/prepare`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${accessToken}`,
        'Idempotency-Key': idempotencyKey,
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
  if (!isGaslessSubmission(body)) {
    throw new Error('The claims API returned an unexpected gasless response')
  }

  return body
}

/**
 * Persists the claimant submitter's verified signature for asynchronous relay.
 *
 * A successful response means the authorization is durable, not necessarily
 * that an Ethereum transaction has already been signed or broadcast.
 */
export async function authorizeGaslessClaim(
  submissionId: string,
  signature: string,
  accessToken: string,
  signal?: AbortSignal,
): Promise<GaslessSubmission> {
  // This endpoint records a submitter signature; it does not broadcast. Durable
  // authorization lets the isolated relayer safely continue after HTTP/browser
  // failure without receiving another signature.
  let response: Response
  try {
    response = await fetch(
      `${API_BASE_URL}/claims/gasless/${encodeURIComponent(submissionId)}/authorize`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ signature }),
        signal,
      },
    )
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not send the claimant authorization to FastAPI.')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isGaslessSubmission(body)) {
    throw new Error('The claims API returned an unexpected gasless response')
  }
  return body
}

/** Reads one submission within the credential that originally created it. */
export async function getGaslessSubmission(
  submissionId: string,
  accessToken: string,
  signal?: AbortSignal,
): Promise<GaslessSubmission> {
  // Status is scoped by the stable subject inside the claimant session. A
  // guessed UUID cannot reveal another person's workflow or public receipt.
  let response: Response
  try {
    response = await fetch(
      `${API_BASE_URL}/claims/gasless/${encodeURIComponent(submissionId)}`,
      {
        headers: { Authorization: `Bearer ${accessToken}` },
        signal,
      },
    )
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not check the sponsored transaction status.')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isGaslessSubmission(body)) {
    throw new Error('The claims API returned an unexpected gasless response')
  }
  return body
}

/**
 * Returns the latest asynchronous model result, or `null` while none exists.
 * A not-found response is part of normal polling; network and schema failures
 * remain exceptions so the UI does not mistake an outage for pending work.
 */
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

/** Authenticates the dedicated human-review console without loading claim data. */
export async function getAssessorSession(
  assessorApiKey: string,
  signal?: AbortSignal,
): Promise<AssessorSession> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/assessor/session`, {
      headers: { 'X-Assessor-API-Key': assessorApiKey },
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not reach the human-assessor service.')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isAssessorSession(body)) {
    throw new Error('The claims API returned an unexpected assessor session')
  }
  return body
}

/** Returns the latest private human outcome, or null before a review exists. */
export async function getAssessorOutcome(
  claimId: number,
  assessorApiKey: string,
  signal?: AbortSignal,
): Promise<AssessorOutcome | null> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/assessor/claims/${claimId}/outcome`, {
      headers: { 'X-Assessor-API-Key': assessorApiKey },
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not load the human assessor outcome.')
  }
  if (response.status === 404) return null
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isAssessorOutcome(body)) {
    throw new Error('The claims API returned an unexpected assessor outcome')
  }
  return body
}

/** Appends a new human-outcome revision; it never changes the on-chain status. */
export async function recordAssessorOutcome(
  claimId: number,
  input: AssessorOutcomeInput,
  assessorApiKey: string,
  signal?: AbortSignal,
): Promise<AssessorOutcome> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/assessor/claims/${claimId}/outcome`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Assessor-API-Key': assessorApiKey,
      },
      body: JSON.stringify(input),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not record the human assessor outcome.')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isAssessorOutcome(body)) {
    throw new Error('The claims API returned an unexpected assessor outcome')
  }
  return body
}

/** Authenticates the proposal-maker boundary; wallet authority is checked later. */
export async function getGovernanceSession(
  governanceApiKey: string,
  signal?: AbortSignal,
): Promise<GovernanceSession> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/governance/session`, {
      headers: { 'X-Governance-API-Key': governanceApiKey },
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not reach the coverage-governance service.')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isGovernanceSession(body)) {
    throw new Error('The claims API returned an unexpected governance session')
  }
  return body
}

/** Persists an audited proposal and returns exact calldata for the checker wallet. */
export async function prepareCoverageDecision(
  claimId: number,
  decisionStatus: CoverageDecisionStatus,
  decisionMakerAddress: string,
  governanceApiKey: string,
  signal?: AbortSignal,
): Promise<CoverageDecisionProposal> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/governance/claims/${claimId}/decision`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Governance-API-Key': governanceApiKey,
      },
      body: JSON.stringify({
        decision_status: decisionStatus,
        decision_maker_address: decisionMakerAddress,
      }),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Could not prepare the coverage decision.')
  }
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new Error(errorMessage(body, response.status))
  if (!isCoverageDecisionProposal(body)) {
    throw new Error('The claims API returned an unexpected decision proposal')
  }
  return body
}

/** Reads one validated page from the PostgreSQL blockchain projection. */
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

/**
 * Loads the authenticated, read-only indexer health and reconciliation view.
 * The raw operations key stays in a request header and is never placed in URLs.
 */
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

/**
 * Searches indexed blockchain events using opaque keyset pagination.
 *
 * `cursor` is generated and validated by FastAPI; the browser deliberately does
 * not decode it. Returning that token unchanged preserves stable pagination even
 * while newly confirmed events are inserted at the head of the audit stream.
 */
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
