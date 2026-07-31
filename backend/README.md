# FastAPI backend

The backend validates schema-version-3 motor claims, stores their canonical JSON
on IPFS, verifies the uploaded bytes, and anchors the hash and pointer on
Sepolia. Kafka performs XGBoost scoring after anchoring, and PostgreSQL supplies
the completed assessment to the browser.

It also provides the paginated claims data used by the React dashboard.

> Submit fictional research test data only. The current IPFS storage is public
> and unencrypted; a CID is an address, not a password.

## Workflow

For `POST /claims`:

1. Validate the request with Pydantic.
2. Create deterministic JSON bytes.
3. Upload the bytes to Pinata and read them back through the IPFS gateway.
4. Calculate the Keccak-256 hash and call `submitClaim` on Sepolia.
5. Return the anchor receipt with `assessment: null`.

The browser polls `GET /claims/{claim_id}/assessment`. The Kafka scoring worker
stores that response and performs the assessment transaction.

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

Add the separate Sepolia submitter and assessor keys plus the Pinata JWT, then
load that same file before running FastAPI and the worker:

```bash
set -a
source .env.local
set +a
```

Backend settings:

| Variable | Required | Purpose |
| --- | :---: | --- |
| `SEPOLIA_RPC_URL` | Yes | RPC endpoint for Ethereum Sepolia |
| `SEPOLIA_SUBMITTER_PRIVATE_KEY` | Writes | Sepolia-only account granted `SUBMITTER_ROLE` |
| `SEPOLIA_ASSESSOR_PRIVATE_KEY` | Worker | Separate Sepolia-only account granted `ASSESSOR_ROLE` for that submitter |
| `CLAIMS_DEPLOYMENT_ID` | Yes | Checked-in Ignition deployment directory; use `sepolia-security-audit-v1` for the hardened contract |
| `RECEIPT_TIMEOUT` | No | Seconds to wait for a transaction receipt |
| `DUPLICATE_FINGERPRINT_KEY` | Async | Private key used for incident HMAC fingerprints; minimum 32 bytes |
| `FRONTEND_ORIGINS` | No | Comma-separated browser origins allowed by CORS |
| `DATABASE_URL` | Async | PostgreSQL assessment store used by the polling endpoint |

IPFS settings:

| Variable | Required | Purpose |
| --- | :---: | --- |
| `PINATA_JWT` | Writes | Server-side Pinata upload credential |
| `IPFS_GATEWAY` | No | Gateway used for the upload round-trip check |

The claims list is a public blockchain read. It needs the Sepolia RPC URL and
deployment artifact, but deliberately does not load the wallet key or Pinata
token. Submitting a new claim still requires both write credentials. At startup,
the API validates the selected local artifact. Blockchain clients additionally
check the RPC chain, bytecode, hardened interface, and wallet role before use.
If required route configuration is missing, FastAPI returns a structured JSON
`503` response instead of an unexplained plain `500`.

Never commit `.env.local`. Write accounts need test ETH and only their intended
contract role. The deployment/admin key is not an application setting.

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
| `GET` | `/claims/{claim_id}/assessment` | Returns the stored XGBoost/SHAP and cross-insurer duplicate result, or 404 while pending |
| `POST` | `/claims` | Validates, stores and anchors a synthetic motor claim |

### Example submission

```bash
curl -X POST http://127.0.0.1:8000/claims \
  -H 'Content-Type: application/json' \
  -d '{
    "insurerId": "northstar-mutual",
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
version, probability, threshold, SHAP reasons, current cross-insurer duplicate
matches, processing error if any, and assessment transaction.

Returning `assessment: null` is not an error. It means the permanent claim anchor
has succeeded and the independent Kafka worker has not finished yet. This split
keeps a slow model or temporary worker restart from holding the submission
request open.

## Test

The tests use in-memory adapters and do not spend test ETH or contact Pinata:

```bash
source backend/.venv/bin/activate
python -m pytest backend/tests -q
```

## Current limitations

- The claims list reads contract state directly and is suitable only for this
  small testnet demonstration.
- The prototype uses process-level wallets rather than a managed signing
  service; submission and assessment are nevertheless separated.
- IPFS content is public and unencrypted.
- The XGBoost model is trained on synthetic data, not real insurance records.
- Duplicate detection uses exact normalized incident fields. It produces review
  candidates, not proof of fraud or privacy-preserving record linkage suitable
  for real insurers.
- Authentication, authorization and rate limiting are not yet implemented.

See the [root project guide](../README.md) for the complete application run and
the [model guide](../model/README.md) for how the fraud score is produced.
