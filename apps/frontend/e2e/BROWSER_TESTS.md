# Frontend browser tests

This directory contains Playwright tests that exercise the application through a
real Chromium browser. Each scenario loads the Vite application, interacts with
the same form controls a user sees, and asserts against the rendered result.

## Test boundary

These are **frontend end-to-end tests with an isolated API boundary**. Playwright
intercepts requests to `/api/**` and returns controlled responses from the test.
Consequently, the tests cover the complete browser workflow—including React
state, request construction, polling, accessibility selectors, and rendering—but
they do not start FastAPI, Kafka, PostgreSQL, IPFS, an Ethereum node, or the
scoring worker.

Keeping those external systems outside this test boundary provides three useful
properties:

- The scenarios run in seconds and do not require credentials or network access.
- Responses and timing are deterministic, including uncommon failure conditions.
- A frontend regression can be distinguished from an infrastructure failure.

The repository's service and integration tests remain responsible for verifying
the real backend and infrastructure boundaries.

## Scenarios

### `duplicate-screening.spec.ts`

Submits a synthetic claim through the public wallet flow and verifies the
following browser contract:

1. The form requests and signs a readable claimant authentication challenge.
2. The short-lived bearer session authorizes preparation and status polling.
3. The claimant signs the exact EIP-712 forward request without receiving an
   insurer credential or gas key.
4. A completed assessment is rendered as a review-only cross-insurer match.
5. The browser produces no unexpected console errors.

### `assessment-polling.spec.ts`

Reproduces a scoring job that remains pending beyond the initial one-minute
polling window. The assessment endpoint returns `404 Not Found` while the worker
has not stored a result, then returns a completed assessment. The test verifies
that the 40.9% score appears without reloading the browser.

The scenario uses a virtual clock only inside the browser test. Production delays
of 2 and 10 seconds are compressed to 1 millisecond while virtual elapsed time
still advances normally. This allows the test to cross the one-minute boundary
quickly without changing application constants.

## Running the tests

Run all browser scenarios from `apps/frontend`:

```bash
npm run test:e2e
```

Run one scenario while developing:

```bash
npm run test:e2e -- assessment-polling.spec.ts
```

Playwright starts an isolated Vite server on `127.0.0.1:4173`. The configuration
is defined in `../playwright.config.ts`. A live local application on port `5173`
is not used by these tests.

When a test fails, Playwright stores diagnostic artifacts under
`apps/frontend/test-results/`. Screenshots and retained video help identify the
visible browser state at the point of failure. CI also records a Playwright trace
on the first retry.

## Adding a scenario

- Intercept every API route that the page is expected to call.
- Abort unexpected requests so missing mocks fail visibly rather than reaching a
  developer's local service.
- Prefer accessible selectors such as roles and labels over CSS selectors.
- Assert the user-visible result and any security-relevant request boundary.
- Keep synthetic credentials and blockchain values obviously non-production.
- Do not depend on test execution order; Playwright runs scenarios in parallel.
