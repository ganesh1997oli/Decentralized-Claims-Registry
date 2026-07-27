# PostgreSQL claim-intelligence storage

This module keeps XGBoost results and claim-specific SHAP reasons outside the
blockchain. Sepolia receives the compact status and score; PostgreSQL keeps the
model version, probability, threshold, reasons, transaction receipt and any
processing error.

It also stores keyed incident fingerprints used to find possible duplicates
across participating synthetic insurers. PostgreSQL never stores the HMAC key,
and the fingerprint is not returned to the browser.

## Idempotency

Every Kafka event has a deterministic `event_id`. PostgreSQL uses it as the
primary key and also allows only one assessment for a chain, contract and claim.
Completed records are not replaced by a replay.

The scoring worker reads the current contract state before writing. If Sepolia
already contains the same status and score, the worker completes the existing
database record instead of submitting another transaction.

## Cross-insurer duplicate matching

`claim_incident_fingerprints` stores one versioned fingerprint per on-chain
claim. Matching is restricted to the same chain and contract and excludes
claims from the current insurer. A PostgreSQL transaction-level advisory lock
serializes equal fingerprints so concurrent submissions cannot silently pass
one another.

The duplicate result is rebuilt when FastAPI reads a claim. This means an
earlier claim can show a match that arrived later. A match remains a review
candidate only and does not alter the XGBoost score or on-chain status.

## Local setup

PostgreSQL is included with the Kafka Compose environment:

```bash
docker compose -f integrations/kafka/compose.yml up -d postgres
cp .env.example .env.local
```

The worker creates the assessment and fingerprint tables and indexes on startup.
Load the connection setting where either FastAPI or the worker needs access:

```bash
set -a
source .env.local
set +a
```

## Test

```bash
source backend/.venv/bin/activate
python -m pytest integrations/postgres/tests -q
```

The isolated tests do not need a running database. The Compose service is for
the real local application flow.

The integration suite creates a uniquely named schema, tests real SQL and
concurrent duplicate submissions, and then removes only that schema:

```bash
TEST_DATABASE_URL=postgresql://claims:claims-local@127.0.0.1:5432/claims_registry \
  python -m pytest integrations/postgres/tests/test_duplicate_integration.py -q
```
