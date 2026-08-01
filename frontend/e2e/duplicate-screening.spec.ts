import { expect, test } from '@playwright/test'

const claimPage = {
  items: [
    {
      claim_id: 7,
      claimant: '0x2222222222222222222222222222222222222222',
      claim_hash: '0xclaimhash',
      data_pointer: 'ipfs://claim-7',
      status: 'UnderReview',
      fraud_score: 4200,
      submitted_at: 1_750_000_000,
      updated_at: 1_750_000_010,
    },
  ],
  page: 1,
  page_size: 10,
  total_items: 1,
  total_pages: 1,
}

const completedAssessment = {
  status: 'UnderReview',
  fraud_score: 4200,
  probability: 0.42,
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
  block_number: 207,
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
}

test('submits a claim and displays a review-only cross-insurer match', async ({
  page,
}) => {
  const consoleErrors: string[] = []
  let submittedPayload: Record<string, unknown> | undefined
  let submittedApiKey: string | undefined
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (request.method() === 'POST' && path === '/api/claims') {
      submittedPayload = request.postDataJSON() as Record<string, unknown>
      submittedApiKey = request.headers()['x-insurer-api-key']
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          claim_id: 7,
          transaction_hash: '0xsubmission',
          block_number: 200,
          data_pointer: 'ipfs://claim-7',
          claim_hash: '0xclaimhash',
          assessment: null,
        }),
      })
      return
    }

    if (request.method() === 'GET' && path === '/api/claims/7/assessment') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(completedAssessment),
      })
      return
    }

    if (request.method() === 'GET' && path === '/api/claims') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(submittedPayload ? claimPage : { ...claimPage, items: [] }),
      })
      return
    }

    await route.abort('failed')
  })

  await page.goto('/')
  await page
    .getByLabel('Synthetic insurer', { exact: true })
    .selectOption('harbour-shield')
  await page
    .getByRole('textbox', { name: /^Insurer API credential/ })
    .fill('local-harbour-shield-api-key-change-me')
  await page.getByLabel('Claim reference').fill('harbour-production-test')
  await page.getByLabel('Policy reference').fill('harbour-policy-test')
  await page.getByRole('button', { name: /Submit synthetic claim/ }).click()

  await expect(
    page.getByRole('heading', { name: 'Possible duplicate incident found' }),
  ).toBeVisible()
  await expect(page.getByText('Claim #3 · Northstar Mutual')).toBeVisible()
  await expect(
    page.getByText(
      'This is a review signal only. Similar synthetic incident details do not prove that either claim is fraudulent.',
    ),
  ).toBeVisible()
  expect(submittedPayload).toMatchObject({
    insurerId: 'harbour-shield',
    claimReference: 'harbour-production-test',
    policyReference: 'harbour-policy-test',
  })
  expect(submittedPayload).not.toHaveProperty('insurerApiKey')
  expect(submittedApiKey).toBe('local-harbour-shield-api-key-change-me')
  expect(consoleErrors).toEqual([])
})
