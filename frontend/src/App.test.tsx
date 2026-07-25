import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App, { ClaimsDashboard, ReceiptCard } from './App.tsx'
import type { ClaimReceipt, ClaimSummary } from './api.ts'
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
