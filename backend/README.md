# FastAPI backend

The backend validates schema-version-2 motor claims, stores their canonical JSON
on IPFS, verifies the uploaded bytes, and anchors the hash and pointer on
Sepolia. In the recommended asynchronous mode, Kafka performs XGBoost scoring
after anchoring and PostgreSQL supplies the completed assessment to the browser.

It also provides the paginated claims data used by the React dashboard.

> Submit synthetic data only. The current IPFS storage is public and
> unencrypted.

## Workflow

For `POST /claims` with `CLAIM_SCORING_MODE="async_xgboost"`:

1. Validate the request with Pydantic.
2. Create deterministic JSON bytes.
3. Upload the bytes to Pinata and read them back through the IPFS gateway.
4. Calculate the Keccak-256 hash and call `submitClaim` on Sepolia.
5. Return the anchor receipt with `assessment: null`.

The browser polls `GET /claims/{claim_id}/assessment`. The Kafka scoring worker
stores that response and performs the assessment transaction. The original
inline demonstration remains available through
`CLAIM_SCORING_MODE="inline_demo"`.

The browser never receives the Pinata JWT or Sepolia private key.

## Install

Run from the repository root:

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
```

## Configure

Create the shared local file from the repository root:

```bash
cp .env.example .env.local
```

Add the Sepolia private key and Pinata JWT, then load that same file before
running FastAPI:

```bash
set -a
source .env.local
set +a
```

Backend settings:

| Variable | Required | Purpose |
| --- | :---: | --- |
| `SEPOLIA_RPC_URL` | Yes | RPC endpoint for Ethereum Sepolia |
| `SEPOLIA_PRIVATE_KEY` | Yes | Fresh Sepolia-only signer authorized as an assessor |
| `MODULE_ID` | No | Ignition artifact ID; defaults to `ClaimsRegistryModule#ClaimsRegistry` |
| `IGNITION_DIR` | No | Alternative Ignition deployment directory |
| `RECEIPT_TIMEOUT` | No | Seconds to wait for a transaction receipt |
| `FRAUD_MODEL_PATH` | No | Alternative compatible model artifact |
| `CLAIM_SCORING_MODE` | No | `inline_demo` or recommended `async_xgboost` |
| `FRONTEND_ORIGINS` | No | Comma-separated browser origins allowed by CORS |
| `DATABASE_URL` | Async | PostgreSQL assessment store used by the polling endpoint |

IPFS settings:

| Variable | Required | Purpose |
| --- | :---: | --- |
| `PINATA_JWT` | Yes | Server-side Pinata upload credential |
| `IPFS_GATEWAY` | No | Gateway used for the upload round-trip check |

Never commit `.env.local`. The signer must contain test ETH. The worker signer
must also have assessor permission in the deployed contract.

## Run

```bash
source backend/.venv/bin/activate
set -a
source .env.local
set +a
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Useful local URLs:

- Health check: <http://127.0.0.1:8000/health>
- Interactive API documentation: <http://127.0.0.1:8000/docs>

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Confirms that the process is running; does not call Sepolia or Pinata |
| `GET` | `/claims?page=1&page_size=10` | Returns current claims newest first; page size is limited to 50 |
| `GET` | `/claims/{claim_id}/assessment` | Returns the stored XGBoost/SHAP result, or 404 while pending |
| `POST` | `/claims` | Validates, stores and anchors a synthetic motor claim |

### Example submission

```bash
curl -X POST http://127.0.0.1:8000/claims \
  -H 'Content-Type: application/json' \
  -d '{
    "claimReference": "synthetic-api-1",
    "policyReference": "synthetic-policy-42",
    "claimType": "collision",
    "incidentDate": "2026-07-13",
    "claimAmountUsd": 2500,
    "policyPremiumUsd": 480,
    "vehicleAge": 6,
    "vehicleType": "sedan",
    "country": "Nigeria",
    "regionType": "urban",
    "thirdPartyInjuryFlag": false,
    "totalLossFlag": false,
    "description": "Synthetic bumper damage for API testing",
    "evidence": []
  }'
```

A successful asynchronous response has HTTP status `201` and includes:

- the claim ID, block number and submission transaction;
- the `ipfs://` pointer and on-chain hash;
- `assessment: null` while Kafka processing is pending.

Once the worker stores a result, the assessment endpoint returns the model
version, probability, threshold, SHAP reasons, processing error if any, and
assessment transaction.

## Test

The tests use in-memory adapters and do not spend test ETH or contact Pinata:

```bash
source backend/.venv/bin/activate
python -m pytest backend/tests -q
```

## Current limitations

- The claims list reads contract state directly and is suitable only for this
  small testnet demonstration.
- One process-level wallet submits and assesses every claim.
- IPFS content is public and unencrypted.
- The XGBoost model is trained on synthetic data, not real insurance records.
- Authentication, authorization and rate limiting are not yet implemented.

See the [root project guide](../README.md) for the complete application run and
the [model guide](../model/README.md) for how the fraud score is produced.
