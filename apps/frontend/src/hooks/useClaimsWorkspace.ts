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

const RAPID_ASSESSMENT_POLL_INTERVAL_MS = 2_000
const RAPID_ASSESSMENT_POLL_WINDOW_MS = 60_000
const PATIENT_ASSESSMENT_POLL_INTERVAL_MS = 10_000

function isAbortError(error: unknown): boolean {
  // Cancellation is expected during navigation and request replacement; keeping
  // this check centralized prevents aborted work from surfacing as user errors.
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
  const [indexedThroughBlock, setIndexedThroughBlock] = useState<number | null>(
    null,
  )
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedClaimId, setSelectedClaimId] = useState<number | null>(null)
  const [openingClaimId, setOpeningClaimId] = useState<number | null>(null)
  const [detailsError, setDetailsError] = useState<string | null>(null)
  const [assessmentPollingError, setAssessmentPollingError] = useState<
    string | null
  >(null)
  const detailsRequest = useRef<AbortController | null>(null)
  // The polling effect owns the request lifecycle; this ref gives the UI a
  // narrow, stable way to ask that effect for an immediate retry.
  const assessmentCheckNow = useRef<(() => void) | null>(null)

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
      // Replace all page metadata from one validated API response. Callers supply
      // cancellation ownership so page changes and component cleanup cannot apply
      // a stale result after a newer navigation.
      setIsLoading(true)
      setError(null)
      try {
        const result = await listClaims(requestedPage, requestedSize, signal)
        setClaims(result.items)
        setPage(result.page)
        setTotalItems(result.total_items)
        setTotalPages(result.total_pages)
        setIndexedThroughBlock(result.indexed_through_block)
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
    // On a fresh page, restore the newest confirmed indexed claim unless the
    // user deliberately selected an older row. PostgreSQL is only a projection;
    // its values originate from confirmed contract events.
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
      // Assessment enrichment is optional here: the confirmed index row is enough
      // to restore details, while a transient assessment read must not erase it.
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
    if (pendingAssessmentClaimId === null) {
      assessmentCheckNow.current = null
      setAssessmentPollingError(null)
      return
    }

    // Submission precedes the Kafka worker. Poll quickly for the normal path,
    // then reduce the request rate without silently abandoning a delayed job.
    const claimId = pendingAssessmentClaimId
    const controller = new AbortController()
    let timer: number | undefined
    let requestInFlight = false
    let immediateCheckQueued = false
    const rapidPollingStartedAt = Date.now()

    function clearScheduledPoll() {
      // Maintain at most one scheduled timer for this claim-specific polling effect.
      if (timer === undefined) return
      window.clearTimeout(timer)
      timer = undefined
    }

    function scheduleNextPoll() {
      // Poll rapidly during normal processing latency, then back off indefinitely.
      // Hidden tabs pause instead of relying on browser-throttled timers.
      if (
        controller.signal.aborted ||
        document.visibilityState === 'hidden'
      ) {
        return
      }

      const rapidWindowIsOpen =
        Date.now() - rapidPollingStartedAt < RAPID_ASSESSMENT_POLL_WINDOW_MS
      const delay = rapidWindowIsOpen
        ? RAPID_ASSESSMENT_POLL_INTERVAL_MS
        : PATIENT_ASSESSMENT_POLL_INTERVAL_MS

      clearScheduledPoll()
      timer = window.setTimeout(() => {
        timer = undefined
        void pollAssessment()
      }, delay)
    }

    function requestImmediatePoll() {
      // Focus events and the manual button may request an early check. Coalesce an
      // in-flight request into one queued follow-up rather than issuing duplicates.
      if (
        controller.signal.aborted ||
        document.visibilityState === 'hidden'
      ) {
        return
      }

      clearScheduledPoll()
      if (requestInFlight) {
        // Do not start overlapping reads. One extra check immediately after
        // the current request is enough to honour a focus event or button click.
        immediateCheckQueued = true
        return
      }
      void pollAssessment()
    }

    async function pollAssessment() {
      // Merge results only into the receipt for this effect's claim. A terminal
      // on-chain result refreshes page one so the projection can show its new state.
      if (
        controller.signal.aborted ||
        requestInFlight ||
        document.visibilityState === 'hidden'
      ) {
        return
      }

      requestInFlight = true
      let assessmentIsTerminal = false
      try {
        const assessment = await getClaimAssessment(claimId, controller.signal)
        setAssessmentPollingError(null)
        if (assessment) {
          setReceipt((current) =>
            current?.claim_id === claimId
              ? { ...current, assessment }
              : current,
          )
          assessmentIsTerminal = assessment.on_chain || Boolean(assessment.error)
          if (assessmentIsTerminal) {
            void loadPage(1, pageSize)
          }
        }
      } catch (pollingError) {
        if (!isAbortError(pollingError)) {
          // A temporary API/network failure must not strand the receipt. Keep
          // retrying at the adaptive interval and tell the user what is happening.
          setAssessmentPollingError(
            'The assessment service could not be reached. Automatic checks will continue.',
          )
        }
      } finally {
        requestInFlight = false
        if (!controller.signal.aborted && !assessmentIsTerminal) {
          if (immediateCheckQueued) {
            immediateCheckQueued = false
            requestImmediatePoll()
          } else {
            scheduleNextPoll()
          }
        }
      }
    }

    function handleVisibilityChange() {
      // Stop timers while hidden and check immediately on return, minimizing both
      // background traffic and perceived staleness after tab restoration.
      if (document.visibilityState === 'hidden') {
        // Background tabs can be heavily throttled. Pause scheduled requests
        // and issue a fresh check as soon as the user returns instead.
        clearScheduledPoll()
      } else {
        requestImmediatePoll()
      }
    }

    setAssessmentPollingError(null)
    assessmentCheckNow.current = requestImmediatePoll
    document.addEventListener('visibilitychange', handleVisibilityChange)
    requestImmediatePoll()

    return () => {
      controller.abort()
      clearScheduledPoll()
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      if (assessmentCheckNow.current === requestImmediatePoll) {
        assessmentCheckNow.current = null
      }
    }
  }, [loadPage, pageSize, pendingAssessmentClaimId])

  const checkPendingAssessment = useCallback(() => {
    // Expose a stable UI action without giving components ownership of timers or
    // the claim-specific AbortController maintained by the polling effect.
    setAssessmentPollingError(null)
    assessmentCheckNow.current?.()
  }, [])

  const showClaimDetails = useCallback(async (claim: ClaimSummary) => {
    // Latest selection wins: abort the previous enrichment request, render public
    // chain state immediately, then merge assessment details only if still selected.
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
      // A mined submission becomes the active detail immediately. Return the list
      // to page one so the listener-projected row appears as soon as it is confirmed.
      detailsRequest.current?.abort()
      setSelectedClaimId(null)
      setDetailsError(null)
      setAssessmentPollingError(null)
      setReceipt(submittedReceipt)
      if (page === 1) void loadPage(1, pageSize)
      else setPage(1)
    },
    [loadPage, page, pageSize],
  )

  const changePageSize = useCallback((nextPageSize: number) => {
    // Page numbers are relative to page size, so changing size always restarts at
    // the newest claims rather than trying to preserve an ambiguous old page.
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
    indexedThroughBlock,
    isLoading,
    error,
    openingClaimId,
    detailsError,
    assessmentPollingError,
    refresh: () => loadPage(page, pageSize),
    checkPendingAssessment,
    showClaimDetails,
    acceptSubmittedReceipt,
    setPage,
    changePageSize,
  }
}
