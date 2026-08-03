// A details card may begin with a submission receipt or with a row read from the
// public contract. This module merges those shapes without inventing a missing
// transaction or database assessment.
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
  chain_state?: ClaimSummary
}

export function receiptFromCurrentClaim(
  claim: ClaimSummary,
  assessment: ClaimAssessment | null,
  currentReceipt?: DisplayReceipt | null,
): DisplayReceipt {
  // Preserve the original submission receipt only when it belongs to this claim;
  // selecting a historical row must never display another claim's transaction.
  const sameClaimReceipt =
    currentReceipt?.claim_id === claim.claim_id ? currentReceipt : null

  return {
    claim_id: claim.claim_id,
    transaction_hash: sameClaimReceipt?.transaction_hash ?? null,
    block_number: sameClaimReceipt?.block_number ?? null,
    data_pointer: claim.data_pointer,
    claim_hash: claim.claim_hash,
    assessment,
    chain_state: claim,
  }
}

export function hasSubmissionReceipt(
  receipt: DisplayReceipt,
): receipt is ClaimReceipt {
  return receipt.transaction_hash !== null && receipt.block_number !== null
}
