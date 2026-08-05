import { expect, test } from '@playwright/test'

/**
 * Browser regression for the asynchronous scoring lifecycle.
 *
 * FastAPI returns 404 from the assessment endpoint until the Kafka worker has
 * persisted a result. This scenario deliberately keeps that endpoint pending
 * beyond the rapid polling window and proves that React continues checking and
 * renders the eventual score without a page reload.
 */
const pendingClaim = {
  claim_id: 8,
  claimant: '0x2222222222222222222222222222222222222222',
  claim_hash: '0xpendingclaimhash',
  data_pointer: 'ipfs://claim-8',
  status: 'Submitted',
  fraud_score: 0,
  submitted_at: 1_750_000_000,
  updated_at: 1_750_000_000,
}

const completedAssessment = {
  status: 'UnderReview',
  fraud_score: 4_090,
  probability: 0.409,
  threshold: 0.47,
  model_version: 'integration-model-v1',
  reasons: [
    {
      feature: 'claim_amount_usd',
      label: 'Claim amount',
      contribution: 0.1,
    },
  ],
  on_chain: true,
  transaction_hash: '0xassessment',
  block_number: 208,
  error: null,
  duplicate_detection: {
    insurer_id: 'harbour-shield',
    fingerprint_version: 'incident-hmac-sha256-v1',
    duplicate_detected: false,
    matches: [],
  },
}

test('shows a delayed assessment without reloading the browser', async ({
  page,
}) => {
  let submitted = false
  let assessmentRequests = 0

  // Preserve the production polling sequence while compressing its delays so
  // the regression remains deterministic and completes in a few seconds. The
  // virtual clock advances by each requested delay, exercising both the rapid
  // and patient polling phases without changing production constants.
  await page.addInitScript(`
    (() => {
      const nativeDateNow = Date.now.bind(Date)
      const nativeSetTimeout = window.setTimeout.bind(window)
      let virtualElapsed = 0

      Date.now = () => nativeDateNow() + virtualElapsed
      window.setTimeout = (handler, timeout = 0, ...args) => {
        if (timeout === 2000 || timeout === 10000) {
          virtualElapsed += timeout
        }
        return nativeSetTimeout(
          handler,
          timeout === 2000 || timeout === 10000 ? 1 : timeout,
          ...args,
        )
      }
    })()
  `)

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    // Submission returns the public blockchain receipt immediately. Assessment
    // remains null because scoring is performed asynchronously after anchoring.
    if (request.method() === 'POST' && path === '/api/claims') {
      submitted = true
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          claim_id: pendingClaim.claim_id,
          transaction_hash: '0xsubmission',
          block_number: 200,
          data_pointer: pendingClaim.data_pointer,
          claim_hash: pendingClaim.claim_hash,
          assessment: null,
        }),
      })
      return
    }

    if (
      request.method() === 'GET' &&
      path === `/api/claims/${pendingClaim.claim_id}/assessment`
    ) {
      assessmentRequests += 1
      // Thirty-one pending responses cross the virtual one-minute rapid window.
      // The next response represents the worker committing its completed result.
      if (assessmentRequests <= 31) {
        await route.fulfill({ status: 404 })
      } else {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(completedAssessment),
        })
      }
      return
    }

    if (request.method() === 'GET' && path === '/api/claims') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: submitted ? [pendingClaim] : [],
          page: 1,
          page_size: 10,
          total_items: submitted ? 1 : 0,
          total_pages: 1,
          indexed_through_block: 11_400_000,
        }),
      })
      return
    }

    // An unexpected API call indicates that this deterministic browser contract
    // is incomplete; fail it rather than contacting a developer's local backend.
    await route.abort('failed')
  })

  await page.goto('/')
  await page
    .getByRole('textbox', { name: /^Insurer API credential/ })
    .fill('local-northstar-mutual-api-key-change-me')
  await page.getByRole('button', { name: /Submit synthetic claim/ }).click()

  // This assertion is intentionally made without page.reload(). It protects the
  // exact user-facing regression where a delayed score appeared only on refresh.
  await expect(page.getByText('40.9%')).toBeVisible()
  expect(assessmentRequests).toBeGreaterThan(31)
})
