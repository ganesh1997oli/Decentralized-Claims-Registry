import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  listClaims,
  submitClaim,
  type ClaimPayload,
  type ClaimPage,
  type ClaimReceipt,
} from './api.ts'

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
  assessment: {
    status: 'Flagged',
    fraud_score: 8500,
    probability: 0.85,
    threshold: 0.3,
    model_version: 'synthetic-logistic-v1',
    reasons: [
      {
        feature: 'high_risk_type',
        label: 'Higher-risk claim category',
        contribution: 1.5,
      },
    ],
    on_chain: true,
    transaction_hash: '0xassessment',
    block_number: 11319479,
    error: null,
  },
}

const claimPage: ClaimPage = {
  items: [
    {
      claim_id: 5,
      claimant: '0x0000000000000000000000000000000000000001',
      claim_hash: '0xhash',
      data_pointer: 'ipfs://bafy-test',
      status: 'UnderReview',
      fraud_score: 1479,
      submitted_at: 1_750_000_000,
      updated_at: 1_750_000_010,
    },
  ],
  page: 2,
  page_size: 5,
  total_items: 6,
  total_pages: 2,
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

describe('listClaims', () => {
  it('returns the validated on-chain claims list', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(claimPage), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(listClaims(2, 5)).resolves.toEqual(claimPage)
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/claims?page=2&page_size=5',
      expect.objectContaining({ signal: undefined }),
    )
  })

  it('rejects an invalid claims-list response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify([{ claim_id: 1 }]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(listClaims()).rejects.toThrow('unexpected claims-list shape')
  })
})
