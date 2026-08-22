// Persist only the latest public receipt. Claim fields and claimant sessions
// never cross this browser-storage boundary.
import { isClaimReceipt, type ClaimReceipt } from './api.ts'

export const LAST_RECEIPT_STORAGE_KEY = 'claims-registry:last-receipt:v1'

function browserStorage(): Storage | null {
  if (typeof window === 'undefined') return null

  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function loadLastReceipt(
  storage: Storage | null = browserStorage(),
): ClaimReceipt | null {
  if (!storage) return null

  try {
    const saved = storage.getItem(LAST_RECEIPT_STORAGE_KEY)
    if (!saved) return null

    const receipt: unknown = JSON.parse(saved)
    if (isClaimReceipt(receipt)) return receipt

    storage.removeItem(LAST_RECEIPT_STORAGE_KEY)
  } catch {
    // Browser storage can be disabled, full, or contain an incomplete old value.
    // The application should still open normally when that happens.
  }

  return null
}

export function saveLastReceipt(
  receipt: ClaimReceipt | null,
  storage: Storage | null = browserStorage(),
): void {
  if (!storage) return

  try {
    if (receipt) {
      storage.setItem(LAST_RECEIPT_STORAGE_KEY, JSON.stringify(receipt))
    } else {
      storage.removeItem(LAST_RECEIPT_STORAGE_KEY)
    }
  } catch {
    // Persistence is a convenience. A storage failure must not break submission.
  }
}
