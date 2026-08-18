import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  authorizeGaslessClaim,
  createClaimantChallenge,
  createClaimantSession,
  getGovernanceSession,
  getGaslessNetwork,
  getGaslessSubmission,
  getClaimAssessment,
  getIndexerOperations,
  listClaims,
  prepareCoverageDecision,
  prepareGaslessClaim,
  searchIndexerEvents,
  type ClaimPayload,
  type ClaimPage,
  type ClaimReceipt,
  type IndexerOperations,
} from './api.ts'

const payload: ClaimPayload = {
  insurerId: 'harbour-shield',
  claimReference: 'synthetic-web-1',
  policyReference: 'synthetic-policy-42',
  claimType: 'collision',
  incidentDate: '2026-07-13',
  claimAmountUsd: 2500,
  policyPremiumUsd: 480,
  vehicleAge: 6,
  vehicleType: 'sedan',
  country: 'Nigeria',
  regionType: 'urban',
  thirdPartyInjuryFlag: false,
  totalLossFlag: false,
  description: 'Synthetic bumper damage',
  evidence: [],
}

const claimantToken = `v1.${'a'.repeat(40)}.${'b'.repeat(43)}`

const receipt: ClaimReceipt = {
  claim_id: 4,
  transaction_hash: '0xtx',
  block_number: 11319478,
  data_pointer: 'ipfs://bafytest',
  claim_hash: `0x${'12'.repeat(32)}`,
  assessment: {
    status: 'Flagged',
    fraud_score: 8500,
    probability: 0.85,
    threshold: 0.3,
    model_version: 'african-motor-xgboost-v1',
    reasons: [
      {
        feature: 'claim_amount_usd',
        label: 'Claim amount',
        contribution: 0.42,
      },
    ],
    on_chain: true,
    transaction_hash: '0xassessment',
    block_number: 11319479,
    error: null,
    duplicate_detection: {
      insurer_id: 'harbour-shield',
      fingerprint_version: 'incident-hmac-sha256-v1',
      duplicate_detected: true,
      matches: [
        {
          claim_id: 3,
          insurer_id: 'northstar-mutual',
        },
      ],
    },
  },
}

const gaslessSubmission = {
  submission_id: '11111111-1111-4111-8111-111111111111',
  state: 'prepared' as const,
  signer_address: '0x1111111111111111111111111111111111111111',
  chain_id: 11155111,
  contract_address: '0x2222222222222222222222222222222222222222',
  forwarder_address: '0x3333333333333333333333333333333333333333',
  claim_hash: `0x${'12'.repeat(32)}`,
  data_pointer: 'ipfs://bafytest',
  deadline: 2_000_000_000,
  typed_data: {
    types: {
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
    },
    primaryType: 'ForwardRequest' as const,
    domain: {
      name: 'ClaimsRegistryForwarder' as const,
      version: '1' as const,
      chainId: 11155111,
      verifyingContract: '0x3333333333333333333333333333333333333333',
    },
    message: {
      from: '0x1111111111111111111111111111111111111111',
      to: '0x2222222222222222222222222222222222222222',
      value: '0',
      gas: '250000',
      nonce: '7',
      deadline: '2000000000',
      data: '0x1234',
    },
  },
  receipt: null,
  error_code: null,
  poll_after_ms: 1500,
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
  indexed_through_block: 11_400_000,
}

const operations: IndexerOperations = {
  state: 'healthy',
  rpc_status: 'available',
  deployment_id: 'sepolia-security-audit-v1',
  chain_id: 11155111,
  contract_address: '0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB',
  confirmation_blocks: 12,
  stale_after_seconds: 120,
  latest_block: 11424295,
  safe_block: 11424283,
  indexed_through_block: 11424283,
  block_lag: 0,
  checkpoint_updated_at: '2026-08-05T12:24:25Z',
  checkpoint_age_seconds: 8,
  total_claims: 7,
  total_events: 12,
  submitted_events: 7,
  assessed_events: 5,
  claim_status_counts: {
    submitted: 2,
    under_review: 1,
    approved: 1,
    rejected: 1,
    flagged: 2,
  },
  recent_events: [
    {
      event_id: '11155111:0xtx:1',
      claim_id: 6,
      event_type: 'ClaimAssessed',
      block_number: 11424280,
      transaction_hash: '0xtx',
      log_index: 1,
      event_timestamp: 1754395200,
      status: 'Flagged',
      fraud_score: 8500,
      indexed_at: '2026-08-05T12:24:20Z',
    },
  ],
  last_reconciliation: {
    indexed_through_block: 11424283,
    chain_claims: 7,
    indexed_claims: 7,
    missing_claim_ids: [],
    unexpected_claim_ids: [],
    mismatched_claim_ids: [],
    consistent: true,
    duration_ms: 132,
    checked_at: '2026-08-05T12:24:25Z',
  },
  observed_at: '2026-08-05T12:24:33Z',
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('gasless claims API', () => {
  it('creates a one-time wallet challenge and claimant session', async () => {
    const challenge = {
      challenge_id: '11111111-1111-4111-8111-111111111111',
      message: 'Sign in to submit an insurance claim',
      expires_at: '2026-08-18T12:05:00Z',
    }
    const session = {
      access_token: claimantToken,
      token_type: 'bearer',
      expires_at: '2026-08-18T12:15:00Z',
      claimant_address: gaslessSubmission.signer_address,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(challenge), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(session), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      createClaimantChallenge(gaslessSubmission.signer_address),
    ).resolves.toEqual(challenge)
    const signature = `0x${'ab'.repeat(65)}`
    await expect(
      createClaimantSession(challenge.challenge_id, signature),
    ).resolves.toEqual(session)

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      walletAddress: gaslessSubmission.signer_address,
    })
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      challenge_id: challenge.challenge_id,
      signature,
    })
  })

  it('loads the server-authoritative wallet network', async () => {
    const network = {
      chain_id: 11155111,
      contract_address: gaslessSubmission.contract_address,
      forwarder_address: gaslessSubmission.forwarder_address,
      domain_name: 'ClaimsRegistryForwarder',
      domain_version: '1',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(network), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(getGaslessNetwork()).resolves.toEqual(network)
  })

  it('prepares a claim with signer and idempotency bindings', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(gaslessSubmission), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      prepareGaslessClaim(
        payload,
        claimantToken,
        'request-123',
      ),
    ).resolves.toEqual(gaslessSubmission)
    expect(fetchMock).toHaveBeenCalledOnce()

    const [url, request] = fetchMock.mock.calls[0]
    expect(url).toBe('http://127.0.0.1:8000/claims/gasless/prepare')
    expect(request).toMatchObject({
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${claimantToken}`,
        'Idempotency-Key': 'request-123',
      },
    })
    expect(JSON.parse(request.body)).toEqual(payload)
  })

  it('authorizes and polls an existing submission', async () => {
    const authorized = { ...gaslessSubmission, state: 'authorized', typed_data: null }
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(authorized), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const signature = `0x${'ab'.repeat(65)}`

    await expect(
      authorizeGaslessClaim(
        gaslessSubmission.submission_id,
        signature,
        claimantToken,
      ),
    ).resolves.toEqual(authorized)
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${claimantToken}`,
      },
    })

    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(authorized), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await expect(
      getGaslessSubmission(gaslessSubmission.submission_id, claimantToken),
    ).resolves.toEqual(authorized)
  })

  it('rejects typed data with fields outside the forwarder protocol', async () => {
    const malicious = {
      ...gaslessSubmission,
      typed_data: {
        ...gaslessSubmission.typed_data,
        types: {
          ...gaslessSubmission.typed_data.types,
          Permit: [{ name: 'spender', type: 'address' }],
        },
      },
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(malicious), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(
      prepareGaslessClaim(
        payload,
        claimantToken,
        'request-123',
      ),
    ).rejects.toThrow('unexpected gasless response')
  })

  it('surfaces FastAPI preparation error details', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'upstream unavailable' }), {
          status: 502,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(
      prepareGaslessClaim(
        payload,
        claimantToken,
        'request-123',
      ),
    ).rejects.toThrow('upstream unavailable')
  })

  it('explains when the backend is offline', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))

    await expect(
      prepareGaslessClaim(
        payload,
        claimantToken,
        'request-123',
      ),
    ).rejects.toThrow('Confirm that the backend is running')
  })
})

describe('getClaimAssessment', () => {
  it('returns null while the worker has not stored an assessment', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 404 })))

    await expect(getClaimAssessment(4)).resolves.toBeNull()
  })

  it('returns the completed assessment', async () => {
    const assessment = receipt.assessment
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(assessment), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(getClaimAssessment(4)).resolves.toEqual(assessment)
  })

  it('rejects an inconsistent duplicate-detection response', async () => {
    const assessment = {
      ...receipt.assessment,
      duplicate_detection: {
        ...receipt.assessment?.duplicate_detection,
        duplicate_detected: false,
      },
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(assessment), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(getClaimAssessment(4)).rejects.toThrow(
      'unexpected assessment shape',
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

describe('getIndexerOperations', () => {
  it('sends the operator key only in a header and validates telemetry', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(operations), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(getIndexerOperations('operator-secret')).resolves.toEqual(
      operations,
    )
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/operations/indexer',
      {
        headers: { 'X-Operations-API-Key': 'operator-secret' },
        signal: undefined,
      },
    )
  })

  it('rejects incomplete operations telemetry', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ state: 'healthy' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(getIndexerOperations('operator-secret')).rejects.toThrow(
      'unexpected operations response',
    )
  })
})

describe('searchIndexerEvents', () => {
  it('sends filters and an opaque cursor without putting the key in the URL', async () => {
    const eventPage = {
      items: operations.recent_events,
      page_size: 10,
      next_cursor: 'next-page',
    }
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(eventPage), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      searchIndexerEvents(
        'operator-secret',
        {
          claimId: 6,
          transactionHash: null,
          eventType: 'ClaimAssessed',
          status: 'Flagged',
          fromBlock: 11_400_000,
          toBlock: 11_500_000,
          limit: 10,
        },
        'current-page',
      ),
    ).resolves.toEqual(eventPage)

    const [url, request] = fetchMock.mock.calls[0]
    expect(url).toContain('/operations/indexer/events?')
    expect(url).toContain('claim_id=6')
    expect(url).toContain('event_type=ClaimAssessed')
    expect(url).toContain('status=Flagged')
    expect(url).toContain('cursor=current-page')
    expect(url).not.toContain('operator-secret')
    expect(request).toMatchObject({
      headers: { 'X-Operations-API-Key': 'operator-secret' },
    })
  })

  it('rejects an incomplete event-search response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(
      searchIndexerEvents('operator-secret', {
        claimId: null,
        transactionHash: null,
        eventType: null,
        status: null,
        fromBlock: null,
        toBlock: null,
        limit: 20,
      }),
    ).rejects.toThrow('unexpected event-search response')
  })
})

describe('coverage governance API', () => {
  it('authenticates a scoped maker and prepares exact checker-wallet calldata', async () => {
    const session = {
      governance_reference: 'northstar-governance-1',
      insurer_address: '0x1111111111111111111111111111111111111111',
    }
    const proposal = {
      decision_id: '11111111-1111-4111-8111-111111111111',
      claim_id: 7,
      decision_status: 'Approved',
      decision_hash: `0x${'12'.repeat(32)}`,
      decision_maker_address: '0x2222222222222222222222222222222222222222',
      proposed_by: session.governance_reference,
      human_outcome_id: '22222222-2222-4222-8222-222222222222',
      human_outcome_revision: 3,
      created_at: '2026-08-18T19:30:00Z',
      confirmed_transaction_hash: null,
      confirmed_at: null,
      chain_id: 11155111,
      contract_address: '0x3333333333333333333333333333333333333333',
      transaction_data: '0x1234',
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(session), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(proposal), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    await expect(getGovernanceSession('maker-secret')).resolves.toEqual(session)
    await expect(
      prepareCoverageDecision(
        7,
        'Approved',
        proposal.decision_maker_address,
        'maker-secret',
      ),
    ).resolves.toEqual(proposal)

    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      headers: { 'X-Governance-API-Key': 'maker-secret' },
    })
    expect(fetchMock.mock.calls[1][0]).toBe(
      'http://127.0.0.1:8000/governance/claims/7/decision',
    )
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      decision_status: 'Approved',
      decision_maker_address: proposal.decision_maker_address,
    })
  })

  it('rejects an incomplete decision proposal before wallet use', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ decision_status: 'Approved' }), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(
      prepareCoverageDecision(
        7,
        'Approved',
        '0x2222222222222222222222222222222222222222',
        'maker-secret',
      ),
    ).rejects.toThrow('unexpected decision proposal')
  })
})
