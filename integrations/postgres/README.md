# PostgreSQL assessment storage

This module keeps XGBoost results and claim-specific SHAP reasons outside the
blockchain. Sepolia receives the compact status and score; PostgreSQL keeps the
model version, probability, threshold, reasons, transaction receipt and any
processing error.

## Idempotency

Every Kafka event has a deterministic `event_id`. PostgreSQL uses it as the
primary key and also allows only one assessment for a chain, contract and claim.
Completed records are not replaced by a replay.

The scoring worker reads the current contract state before writing. If Sepolia
already contains the same status and score, the worker completes the existing
database record instead of submitting another transaction.

## Local setup

PostgreSQL is included with the Kafka Compose environment:

```bash
docker compose -f integrations/kafka/compose.yml up -d postgres
cp .env.example .env.local
```

The worker creates the table and index on startup. Load the connection setting
where either FastAPI or the worker needs assessment access:

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
