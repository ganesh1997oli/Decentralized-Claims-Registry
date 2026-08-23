import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App, {
  ClaimsDashboard,
  IndexerOperationsView,
  ReceiptCard,
} from './App.tsx'
import { AssessorOutcomeDashboard } from './components/AssessorOutcomeDashboard.tsx'
import { IndexerOperationsDashboard } from './components/IndexerOperationsDashboard.tsx'
import type {
  ClaimReceipt,
  ClaimSummary,
  IndexerOperations,
} from './api.ts'
import { receiptFromCurrentClaim } from './display-receipt.ts'

const LAST_RECEIPT_STORAGE_KEY = 'claims-registry:last-receipt:v1'

const completedReceipt: ClaimReceipt = {
  claim_id: 9,
  transaction_hash: '0xsubmission',
  block_number: 11_348_385,
  data_pointer: 'ipfs://bafy-last-claim',
  claim_hash: '0xclaim',
  assessment: {
    status: 'UnderReview',
    fraud_score: 3_930,
    probability: 0.393,
    threshold: 0.47,
    model_version: 'african-motor-xgboost-v1',
    reasons: [
      {
        feature: 'claim_amount_usd',
        label: 'Claim amount',
        contribution: 0.18,
      },
    ],
    on_chain: true,
    transaction_hash: '0xassessment',
    block_number: 11_348_390,
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

function createMemoryStorage(): Storage {
  const values = new Map<string, string>()

  return {
    get length() {
      return values.size
    },
    clear() {
      values.clear()
    },
    getItem(key) {
      return values.get(key) ?? null
    },
    key(index) {
      return [...values.keys()][index] ?? null
    },
    removeItem(key) {
      values.delete(key)
    },
    setItem(key, value) {
      values.set(key, value)
    },
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App refresh recovery', () => {
  it('restores the last public receipt and screening after a page reload', () => {
    const storage = createMemoryStorage()
    storage.setItem(
      LAST_RECEIPT_STORAGE_KEY,
      JSON.stringify(completedReceipt),
    )
    vi.stubGlobal('window', { localStorage: storage })

    const page = renderToStaticMarkup(<App />)

    expect(page).toContain('Claim #9 is anchored')
    expect(page).toContain('african-motor-xgboost-v1')
    expect(page).toContain('Claim amount')
    expect(page).toContain('3,930')
    expect(page).toContain('Possible duplicate incident found')
    expect(page).toContain('Claim #3')
    expect(page).toContain('Northstar Mutual')
  })

  it('clearly labels the form as research test data', () => {
    const page = renderToStaticMarkup(<App />)

    expect(page).toContain('Research test data only')
    expect(page).not.toContain('Synthetic data only')
  })
})

const historicalClaim: ClaimSummary = {
  claim_id: 5,
  claimant: '0x0000000000000000000000000000000000000001',
  claim_hash: '0xhistorical',
  data_pointer: 'ipfs://bafy-historical',
  status: 'Flagged',
  fraud_score: 3_909,
  submitted_at: 1_753_459_420,
  updated_at: 1_753_459_500,
}

describe('Historical claim details', () => {
  it('makes every listed claim selectable', () => {
    const page = renderToStaticMarkup(
      <ClaimsDashboard
        claims={[historicalClaim]}
        page={1}
        pageSize={10}
        totalItems={1}
        totalPages={1}
        indexedThroughBlock={11_400_000}
        isLoading={false}
        error={null}
        selectedClaimId={5}
        openingClaimId={null}
        onRefresh={() => undefined}
        onClaimSelect={() => undefined}
        onPageChange={() => undefined}
        onPageSizeChange={() => undefined}
      />,
    )

    expect(page).toContain('aria-label="View details for claim 5"')
    expect(page).toContain('aria-current="true"')
    expect(page).toContain('View details')
  })

  it('shows the on-chain score when an older model record is unavailable', () => {
    const page = renderToStaticMarkup(
      <ReceiptCard receipt={receiptFromCurrentClaim(historicalClaim, null)} />,
    )

    expect(page).toContain('Claim #5 is anchored')
    expect(page).toContain('On-chain screening recorded')
    expect(page).toContain('3,909')
    expect(page).toContain('SHAP indicators are')
  })
})

const operationsSnapshot: IndexerOperations = {
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

describe('Indexer operations dashboard', () => {
  it('renders health, lag, reconciliation and recent event evidence', () => {
    const page = renderToStaticMarkup(
      <IndexerOperationsView
        snapshot={operationsSnapshot}
        isRefreshing={false}
        error={null}
        eventPage={{
          items: operationsSnapshot.recent_events,
          page_size: 20,
          next_cursor: null,
        }}
        eventPageNumber={1}
        isSearchingEvents={false}
        eventError={null}
        onRefresh={() => undefined}
        onDisconnect={() => undefined}
        onEventSearch={() => undefined}
        onOlderEvents={() => undefined}
        onNewerEvents={() => undefined}
      />,
    )

    expect(page).toContain('Blockchain indexer operations')
    expect(page).toContain('Healthy')
    expect(page).toContain('Block lag')
    expect(page).toContain('Consistent')
    expect(page).toContain('Event explorer')
    expect(page).toContain('Claim or transaction')
    expect(page).toContain('Search events')
    expect(page).toContain('Claim assessed')
    expect(page).toContain('11,424,283')
  })
})

describe('Public read-only demonstration', () => {
  it('opens the assessor surface without rendering a credential form', () => {
    const page = renderToStaticMarkup(
      <AssessorOutcomeDashboard publicDemoReadOnly />,
    )

    expect(page).toContain('Public read-only demo')
    expect(page).toContain('Production requires an assessor API key')
    expect(page).not.toContain('Assessor API key')
  })

  it('opens operations without rendering a credential form', () => {
    const page = renderToStaticMarkup(
      <IndexerOperationsDashboard publicDemoReadOnly />,
    )

    expect(page).toContain('Public read-only demo')
    expect(page).toContain('Production requires an operations API key')
    expect(page).not.toContain('Operations API key')
  })

  it('labels the writable assessor prototype without requesting a key', () => {
    const page = renderToStaticMarkup(
      <AssessorOutcomeDashboard
        publicDemoReadOnly
        publicPrototypeAssessor
      />,
    )

    expect(page).toContain('Public research prototype')
    expect(page).toContain('Production requires an assessor API key')
    expect(page).not.toContain('Assessor API key')
    expect(page).not.toContain('Recording is disabled')
  })
})
