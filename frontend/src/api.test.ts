import { afterEach, describe, expect, it, vi } from 'vitest'
import { submitClaim, type ClaimPayload, type ClaimReceipt } from './api.ts'

const payload: ClaimPayload = {
  claimReference: 'synthetic-web-1',
  policyReference: 'synthetic-policy-42',
  claimType: 'vehicle_damage',
  incidentDate: '2026-07-13',
  amountPence: 250000,
  description: 'Synthetic bumper damage',
  evidence: [],
}

const receipt: ClaimReceipt = {
  claim_id: 4,
  transaction_hash: '0xtx',
  block_number: 11319478,
  data_pointer: 'ipfs://bafy-test',
  claim_hash: '0xhash',
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('submitClaim', () => {
  it('posts the claim and returns a validated receipt', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(receipt), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(submitClaim(payload)).resolves.toEqual(receipt)
    expect(fetchMock).toHaveBeenCalledOnce()

    const [url, request] = fetchMock.mock.calls[0]
    expect(url).toBe('http://127.0.0.1:8000/claims')
    expect(request).toMatchObject({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    })
    expect(JSON.parse(request.body)).toEqual(payload)
  })

  it('surfaces FastAPI error details', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'upstream unavailable' }), {
          status: 502,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(submitClaim(payload)).rejects.toThrow('upstream unavailable')
  })

  it('explains when the backend is offline', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))

    await expect(submitClaim(payload)).rejects.toThrow(
      'Confirm that the backend is running',
    )
  })
})
