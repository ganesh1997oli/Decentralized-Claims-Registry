import { describe, expect, it } from 'vitest'
import type { ClaimAssessment, ClaimSummary } from './api.ts'
import {
  hasSubmissionReceipt,
  receiptFromCurrentClaim,
} from './display-receipt.ts'

const claim: ClaimSummary = {
  claim_id: 9,
  claimant: '0x0000000000000000000000000000000000000001',
  claim_hash: '0xclaim',
  data_pointer: 'ipfs://bafy-latest',
  status: 'UnderReview',
  fraud_score: 3_930,
  submitted_at: 1_753_459_420,
  updated_at: 1_753_459_500,
}

const assessment: ClaimAssessment = {
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
}

describe('current claim receipt', () => {
  it('rebuilds the public screening when browser storage is empty', () => {
    const receipt = receiptFromCurrentClaim(claim, assessment)

    expect(receipt).toMatchObject({
      claim_id: 9,
      data_pointer: 'ipfs://bafy-latest',
      claim_hash: '0xclaim',
      assessment,
    })
    expect(hasSubmissionReceipt(receipt)).toBe(false)
  })
})
