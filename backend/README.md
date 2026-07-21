# FastAPI claim-submission backend (proposal Week 3)

This service validates a synthetic claim, serializes it deterministically,
uploads the exact JSON bytes to public IPFS through Pinata, verifies the IPFS
round trip, and submits the resulting Keccak-256 hash and `ipfs://` pointer to
the deployed Sepolia `ClaimsRegistry` contract. Week 5 then runs the versioned
synthetic logistic model and records either `UnderReview` or `Flagged` in a
second Sepolia transaction.

It is a dissertation prototype. Public IPFS is not appropriate for personal or
confidential claim data, so submit synthetic data only.

## Install and test

Run from the repository root:

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
pytest backend/tests -q
```

## Configure and run

```bash
cp backend/.env.example backend/.env.local
# Fill in the Sepolia RPC URL, Sepolia-only private key, and Pinata JWT.
set -a; source backend/.env.local; set +a
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/docs> for the interactive API documentation.

The service reads the contract address and ABI from
`contract/ignition/deployments/chain-11155111` by default.

## Submit a synthetic claim

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

A successful request returns HTTP `201` with the claim ID, transaction hash,
block number, IPFS pointer, on-chain claim hash, model output, compact reasons,
and assessment transaction. Run
`listener/claims_listener.py` in another terminal to observe and independently
verify both `ClaimSubmitted` and `ClaimAssessed`.

If claim anchoring succeeds but the assessment transaction fails, the API still
returns HTTP `201` with `assessment.on_chain=false`. This avoids a browser retry
creating a duplicate claim. Record that claim ID for an operational assessment
retry rather than submitting it again.

## API endpoints

- `GET /health` - process liveness; does not call Pinata or Sepolia.
- `GET /claims?page=1&page_size=10` - read a newest-first page of current claim
  status and fraud scores from Sepolia. `page_size` must be between 1 and 50.
- `POST /claims` - validate, upload, verify, and submit a synthetic claim.

The paginated response contains `items`, `page`, `page_size`, `total_items`,
and `total_pages`. The endpoint calls `claimCount()` and then `getClaim(id)`
only for the requested IDs. This direct read is appropriate for the small
testnet demonstration. Use an indexed store or The Graph before operating at
production scale.

## Week 5 model boundary

The default artifact lives at
`model/artifacts/synthetic-logistic-v1.json`. Override it with
`FRAUD_MODEL_PATH` when testing another compatible artifact. Regenerate the
tracked baseline from the repository root with:

```bash
python -m model.train
```

The local reasons are logistic feature contributions, not SHAP values. The
artifact is intentionally synthetic and demonstrates integration only.

For production, replace direct private-key signing with a managed relayer and
encrypt sensitive claim data before storing it off-chain.
