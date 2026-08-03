// Single orchestration point for cancellable reads, pagination, claim selection,
// assessment polling, and the latest public receipt kept by the browser.
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getClaimAssessment,
  listClaims,
  type ClaimReceipt,
  type ClaimSummary,
} from '../api.ts'
import {
  hasSubmissionReceipt,
  receiptFromCurrentClaim,
  type DisplayReceipt,
} from '../display-receipt.ts'
import { loadLastReceipt, saveLastReceipt } from '../receipt-storage.ts'

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

/**
 * Coordinates the claims list, selected details, and asynchronous assessment.
 *
 * Components consume one cohesive workspace instead of reproducing request
 * cancellation, polling, pagination, and receipt persistence independently.
 */
export function useClaimsWorkspace() {
  const [receipt, setReceipt] = useState<DisplayReceipt | null>(() =>
    loadLastReceipt(),
  )
  const [claims, setClaims] = useState<ClaimSummary[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [totalItems, setTotalItems] = useState(0)
  const [totalPages, setTotalPages] = useState(1)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedClaimId, setSelectedClaimId] = useState<number | null>(null)
  const [openingClaimId, setOpeningClaimId] = useState<number | null>(null)
  const [detailsError, setDetailsError] = useState<string | null>(null)
  const detailsRequest = useRef<AbortController | null>(null)

  const pendingAssessmentClaimId =
    receipt &&
    !receipt.assessment?.on_chain &&
    !receipt.assessment?.error &&
    (!receipt.chain_state || receipt.chain_state.status === 'Submitted')
      ? receipt.claim_id
      : null

  useEffect(() => {
    // Historical selections do not have an original submission transaction.
    // Preserve the latest complete browser receipt instead of overwriting it.
    if (receipt && hasSubmissionReceipt(receipt)) saveLastReceipt(receipt)
  }, [receipt])

  useEffect(
    () => () => {
      detailsRequest.current?.abort()
    },
    [],
  )

  const loadPage = useCallback(
    async (requestedPage: number, requestedSize: number, signal?: AbortSignal) => {
      setIsLoading(true)
      setError(null)
      try {
        const result = await listClaims(requestedPage, requestedSize, signal)
        setClaims(result.items)
        setPage(result.page)
        setTotalItems(result.total_items)
        setTotalPages(result.total_pages)
      } catch (loadingError) {
        if (isAbortError(loadingError)) return
        setError(
          loadingError instanceof Error
            ? loadingError.message
            : 'The claims list could not be loaded.',
        )
      } finally {
        setIsLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    const controller = new AbortController()
    void loadPage(page, pageSize, controller.signal)
    return () => controller.abort()
  }, [loadPage, page, pageSize])

  useEffect(() => {
    // On a fresh page, the contract list is the source of truth. Restore the
    // newest claim unless the user deliberately selected an older row.
    if (
      selectedClaimId !== null ||
      page !== 1 ||
      isLoading ||
      claims.length === 0
    ) {
      return
    }

    const latestClaim = claims[0]
    if (receipt && receipt.claim_id >= latestClaim.claim_id) return

    const controller = new AbortController()
    async function restoreLatestClaim() {
      let assessment = null
      try {
        assessment = await getClaimAssessment(
          latestClaim.claim_id,
          controller.signal,
        )
      } catch (loadingError) {
        if (isAbortError(loadingError)) return
      }

      if (controller.signal.aborted) return
      const restored = receiptFromCurrentClaim(latestClaim, assessment)
      setReceipt((current) =>
        current && current.claim_id >= latestClaim.claim_id ? current : restored,
      )
    }

    void restoreLatestClaim()
    return () => controller.abort()
  }, [claims, isLoading, page, receipt, selectedClaimId])

  useEffect(() => {
    if (pendingAssessmentClaimId === null) return

    // Submission precedes the Kafka worker. Poll for at most one minute so the
    // UI can transition from "anchored" to its final XGBoost/SHAP assessment.
    const claimId = pendingAssessmentClaimId
    const controller = new AbortController()
    let timer: number | undefined
    let attempts = 0

    async function pollAssessment() {
      try {
        const assessment = await getClaimAssessment(claimId, controller.signal)
        if (assessment) {
          setReceipt((current) =>
            current?.claim_id === claimId
              ? { ...current, assessment }
              : current,
          )
          if (assessment.on_chain || assessment.error) {
            void loadPage(1, pageSize)
            return
          }
        }
      } catch (pollingError) {
        if (isAbortError(pollingError)) return
      }

      attempts += 1
      if (attempts < 30 && !controller.signal.aborted) {
        timer = window.setTimeout(pollAssessment, 2_000)
      }
    }

    void pollAssessment()
    return () => {
      controller.abort()
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [loadPage, pageSize, pendingAssessmentClaimId])

  const showClaimDetails = useCallback(async (claim: ClaimSummary) => {
    detailsRequest.current?.abort()
    const controller = new AbortController()
    detailsRequest.current = controller

    setSelectedClaimId(claim.claim_id)
    setOpeningClaimId(claim.claim_id)
    setDetailsError(null)

    // Contract values render immediately while the richer PostgreSQL assessment
    // is loading, which preserves feedback even when that dependency is slow.
    setReceipt((current) => receiptFromCurrentClaim(claim, null, current))
    window.requestAnimationFrame(() => {
      document
        .getElementById('claim-details')
        ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })

    try {
      const assessment = await getClaimAssessment(claim.claim_id, controller.signal)
      setReceipt((current) =>
        current?.claim_id === claim.claim_id
          ? receiptFromCurrentClaim(claim, assessment, current)
          : current,
      )
    } catch (loadingError) {
      if (isAbortError(loadingError)) return
      setDetailsError(
        loadingError instanceof Error
          ? loadingError.message
          : 'The fraud-screening details could not be loaded.',
      )
    } finally {
      setOpeningClaimId((current) =>
        current === claim.claim_id ? null : current,
      )
      if (detailsRequest.current === controller) detailsRequest.current = null
    }
  }, [])

  const acceptSubmittedReceipt = useCallback(
    (submittedReceipt: ClaimReceipt) => {
      detailsRequest.current?.abort()
      setSelectedClaimId(null)
      setDetailsError(null)
      setReceipt(submittedReceipt)
      if (page === 1) void loadPage(1, pageSize)
      else setPage(1)
    },
    [loadPage, page, pageSize],
  )

  const changePageSize = useCallback((nextPageSize: number) => {
    setPage(1)
    setPageSize(nextPageSize)
  }, [])

  return {
    receipt,
    claims,
    page,
    pageSize,
    totalItems,
    totalPages,
    isLoading,
    error,
    openingClaimId,
    detailsError,
    refresh: () => loadPage(page, pageSize),
    showClaimDetails,
    acceptSubmittedReceipt,
    setPage,
    changePageSize,
  }
}
