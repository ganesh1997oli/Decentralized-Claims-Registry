import { describe, expect, it } from 'vitest'
import type { ClaimReceipt } from './api.ts'
import {
  LAST_RECEIPT_STORAGE_KEY,
  loadLastReceipt,
  saveLastReceipt,
} from './receipt-storage.ts'

function receipt(claimId: number): ClaimReceipt {
  return {
    claim_id: claimId,
    transaction_hash: `0xsubmission-${claimId}`,
    block_number: 11_348_000 + claimId,
    data_pointer: `ipfs://claim-${claimId}`,
    claim_hash: `0xclaim-${claimId}`,
    assessment: null,
  }
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

describe('last receipt storage', () => {
  it('replaces the previous receipt when a newer claim succeeds', () => {
    const storage = createMemoryStorage()

    saveLastReceipt(receipt(9), storage)
    saveLastReceipt(receipt(10), storage)

    expect(loadLastReceipt(storage)?.claim_id).toBe(10)
  })

  it('ignores and removes an invalid saved value', () => {
    const storage = createMemoryStorage()
    storage.setItem(LAST_RECEIPT_STORAGE_KEY, '{"claim_id":"invalid"}')

    expect(loadLastReceipt(storage)).toBeNull()
    expect(storage.getItem(LAST_RECEIPT_STORAGE_KEY)).toBeNull()
  })
})
