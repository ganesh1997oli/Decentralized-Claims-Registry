import type {
  ClaimAssessment,
  ClaimReceipt,
  ClaimSummary,
} from './api.ts'

export type DisplayReceipt = Omit<
  ClaimReceipt,
  'transaction_hash' | 'block_number'
> & {
  transaction_hash: string | null
  block_number: number | null
}

export function receiptFromCurrentClaim(
  claim: ClaimSummary,
  assessment: ClaimAssessment | null,
): DisplayReceipt {
  return {
    claim_id: claim.claim_id,
    transaction_hash: null,
    block_number: null,
    data_pointer: claim.data_pointer,
    claim_hash: claim.claim_hash,
    assessment,
  }
}

export function hasSubmissionReceipt(
  receipt: DisplayReceipt,
): receipt is ClaimReceipt {
  return receipt.transaction_hash !== null && receipt.block_number !== null
}
