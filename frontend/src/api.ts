export type ClaimPayload = {
  claimReference: string
  policyReference: string
  claimType: string
  incidentDate: string
  amountPence: number
  description: string
  evidence: string[]
}

export type ClaimReceipt = {
  claim_id: number
  transaction_hash: string
  block_number: number
  data_pointer: string
  claim_hash: string
}

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'
).replace(/\/$/, '')

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isClaimReceipt(value: unknown): value is ClaimReceipt {
  if (!isRecord(value)) return false

  return (
    typeof value.claim_id === 'number' &&
    typeof value.transaction_hash === 'string' &&
    typeof value.block_number === 'number' &&
    typeof value.data_pointer === 'string' &&
    typeof value.claim_hash === 'string'
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
