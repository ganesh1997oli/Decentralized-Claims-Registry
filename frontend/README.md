# React frontend

The frontend provides a simple interface for submitting a fictional motor-claim
test case and reviewing claims already recorded on Sepolia. It calls FastAPI for
every operation; it does not connect directly to a wallet, Pinata, Kafka, or the
model. Keeping those integrations server-side is why secrets never need to enter
browser code.

## What the interface shows

- A research test claim-submission form
- The confirmed Sepolia transaction and block
- The IPFS pointer and claim hash
- A pending state while Kafka processes the anchored claim
- The cross-insurer duplicate-review result and any matched claim IDs
- The XGBoost probability and claim-specific SHAP indicators from PostgreSQL
- The on-chain assessment status and transaction
- A newest-first, paginated list of submitted claims and fraud scores
- A selectable details view for every claim in the Sepolia claims list

The page offers claim-list sizes of 5, 10, 25, or 50.

## Source layout

`App.tsx` is intentionally only the page composition shell. `ClaimForm` owns
credential-in-memory form state and validation, while `ReceiptCard` and
`ClaimsDashboard` own presentation. `useClaimsWorkspace` is the single boundary
for cancellation, list pagination, detail loading, assessment polling, and
receipt persistence. Shared public insurer labels and IPFS-link formatting live
in `claim-display.ts`; no component owns authorization policy.

## Install

Run from the repository root:

```bash
npm --prefix frontend ci
```

Use `npm --prefix frontend install` instead if you intentionally need to update
dependencies or the lock file.

## Configure

From the repository root, create the same local file used by the backend:

```bash
cp .env.example .env.local
set -a
source .env.local
set +a
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000` | FastAPI base URL |
| `VITE_IPFS_GATEWAY` | `https://gateway.pinata.cloud/ipfs` | Opens receipt CIDs in a browser |

Vite variables are visible in the browser. Never add an insurer API key, wallet
private key, Pinata JWT, claim-authorization key, or other secret to a `VITE_`
variable.

## Run locally

Start the FastAPI backend first. From the repository root, load the shared file
in this terminal and run:

```bash
set -a
source .env.local
set +a
npm --prefix frontend run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>.

The form begins with clearly labelled synthetic values. After a successful
submission, the receipt links to Etherscan and the configured IPFS gateway. In
asynchronous mode it polls FastAPI for up to one minute while Kafka scores the
claim, then displays the duplicate check, stored assessment and refreshed
contract state.
The form also asks for the selected synthetic insurer's API key. That value is
sent only in `X-Insurer-API-Key`, remains in React runtime memory, and is cleared
when the form resets. FastAPI derives the authoritative insurer from the key and
rejects a selection that does not match. The local-only example keys are listed
in the [backend guide](../backend/README.md#local-insurer-credentials).
The header labels the interface **Research test data only** to make clear that
users must enter fictional test claims; it does not describe where the research
dataset is hosted.

The browser keeps the latest successful public receipt so its Sepolia details,
XGBoost score, and SHAP indicators remain visible after a page refresh. A newer
successful claim replaces it. If that browser receipt is unavailable, the page
rebuilds the panel from the newest Sepolia claim and its FastAPI assessment.
Browser storage contains no form fields, wallet key, or Pinata credential and
can be cleared through the browser's site-data tools.

Select any claim number in **All submitted claims** to reopen its Sepolia hash,
IPFS pointer, on-chain score, duplicate check, model result, and SHAP indicators.
Older claims created before the current PostgreSQL assessment history may only
have their on-chain status and score; the page labels that limitation instead
of inventing missing XGBoost, duplicate or SHAP details.

## Verify the frontend

```bash
cd frontend
npm test
npm run lint
npm run build
npx playwright install chromium
npm run test:e2e
```

- `npm test` checks the backend client and response validation.
- `npm run lint` checks the source for common mistakes.
- `npm run build` runs TypeScript compilation and creates the production bundle.
- `npm run test:e2e` opens the application in Chromium and verifies submission,
  assessment polling and the review-only cross-insurer match experience.

## Safety and limitations

- Enter fictional test information only. Do not use real policyholder data.
- Evidence uploads are intentionally absent while storage is public and
  unencrypted. Hiding a CID would not make a photograph or document private.
- The displayed XGBoost score comes from synthetic training data and must not be
  used to decide a real claim.
- A matching incident fingerprint is only a human-review signal; it does not
  establish that either synthetic claim is fraudulent.
- The dashboard reads current Sepolia state through FastAPI; it is not a
  production search or reporting system.

See the [backend guide](../backend/README.md) for the API setup and the
[root project guide](../README.md) for the complete run order.
