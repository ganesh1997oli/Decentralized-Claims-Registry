# React frontend

The frontend provides a simple interface for submitting a synthetic motor claim
and reviewing claims already recorded on Sepolia. It calls FastAPI for every
operation; it does not connect directly to a wallet, Pinata, Kafka, or the model.

## What the interface shows

- A synthetic claim-submission form
- The confirmed Sepolia transaction and block
- The IPFS pointer and claim hash
- The demonstration fraud probability and contributing indicators
- The on-chain assessment status and transaction
- A newest-first, paginated list of submitted claims and fraud scores

The page offers claim-list sizes of 5, 10, 25, or 50.

## Install

```bash
cd frontend
npm ci
```

Use `npm install` instead if you intentionally need to update dependencies or the
lock file.

## Configure

```bash
cp .env.example .env.local
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000` | FastAPI base URL |
| `VITE_IPFS_GATEWAY` | `https://gateway.pinata.cloud/ipfs` | Opens receipt CIDs in a browser |

Vite variables are visible in the browser. Never add a private key, Pinata JWT,
or other secret to a `VITE_` variable.

## Run locally

Start the FastAPI backend first, then run:

```bash
cd frontend
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>.

The form begins with clearly labelled synthetic values. After a successful
submission, the receipt links to Etherscan and the configured IPFS gateway, and
the claims list refreshes automatically.

## Verify the frontend

```bash
cd frontend
npm test
npm run lint
npm run build
```

- `npm test` checks the backend client and response validation.
- `npm run lint` checks the source for common mistakes.
- `npm run build` runs TypeScript compilation and creates the production bundle.

## Safety and limitations

- Enter synthetic information only.
- Evidence uploads are intentionally absent while storage is public and
  unencrypted.
- The displayed score comes from a demonstration model and must not be used to
  decide a real claim.
- The dashboard reads current Sepolia state through FastAPI; it is not a
  production search or reporting system.

See the [backend guide](../backend/README.md) for the API setup and the
[root project guide](../README.md) for the complete run order.
