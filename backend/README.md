# FastAPI backend

The backend coordinates the complete claim-submission workflow. It validates the
request, scores the synthetic claim, stores its canonical JSON on IPFS, verifies
the uploaded bytes, anchors the hash and pointer on Sepolia, and writes the model
result back to the contract.

It also provides the paginated claims data used by the React dashboard.

> Submit synthetic data only. The current IPFS storage is public and
> unencrypted.

## Workflow

For `POST /claims`, the backend performs these steps in order:

1. Validate the request with Pydantic.
2. Create deterministic JSON bytes.
3. Calculate a probability with the local synthetic model.
4. Upload the bytes to Pinata and read them back through the IPFS gateway.
5. Calculate the Keccak-256 hash and call `submitClaim` on Sepolia.
6. Call `assessClaim` with `UnderReview` or `Flagged` and the score in basis
   points.
7. Return the submission and assessment receipts to the browser.

The browser never receives the Pinata JWT or Sepolia private key.

## Install

Run from the repository root:

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
```

## Configure

```bash
cp backend/.env.example backend/.env.local
```

Edit `.env.local`, then load it into each terminal that runs the backend:

```bash
set -a; source backend/.env.local; set +a
```

| Variable | Required | Purpose |
| --- | :---: | --- |
| `SEPOLIA_RPC_URL` | Yes | RPC endpoint for Ethereum Sepolia |
| `SEPOLIA_PRIVATE_KEY` | Yes | Fresh Sepolia-only signer authorized as an assessor |
| `PINATA_JWT` | Yes | Server-side Pinata upload credential |
| `IPFS_GATEWAY` | No | Gateway used for the upload round-trip check |
| `MODULE_ID` | No | Ignition artifact ID; defaults to `ClaimsRegistryModule#ClaimsRegistry` |
| `IGNITION_DIR` | No | Alternative Ignition deployment directory |
| `RECEIPT_TIMEOUT` | No | Seconds to wait for a transaction receipt |
| `FRAUD_MODEL_PATH` | No | Alternative compatible model artifact |
| `FRONTEND_ORIGINS` | No | Comma-separated browser origins allowed by CORS |

Never commit `.env.local`. The signer must contain test ETH and must have
assessor permission in the deployed contract.

## Run

```bash
source backend/.venv/bin/activate
set -a; source backend/.env.local; set +a
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
| `POST` | `/claims` | Validates, scores, stores and anchors a synthetic claim |

### Example submission

```bash
curl -X POST http://127.0.0.1:8000/claims \
  -H 'Content-Type: application/json' \
  -d '{
    "claimReference": "synthetic-api-1",
    "policyReference": "synthetic-policy-42",
    "claimType": "vehicle_damage",
    "incidentDate": "2026-07-13",
    "amountPence": 250000,
    "description": "Synthetic bumper damage for API testing",
    "evidence": []
  }'
```

A successful response has HTTP status `201` and includes:

- the claim ID, block number and submission transaction;
- the `ipfs://` pointer and on-chain hash;
- the model version, probability, threshold and contributing reasons;
- the assessment status, fraud score and assessment transaction.

If anchoring succeeds but the assessment transaction fails, the response still
returns the successful claim receipt with `assessment.on_chain` set to `false`.
This prevents a browser retry from creating a duplicate claim.

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
- The model is trained on deterministic synthetic data, not real insurance
  records.
- Authentication, authorization, audit storage and rate limiting are not yet
  implemented.

See the [root project guide](../README.md) for the complete application run and
the [model guide](../model/README.md) for how the fraud score is produced.
