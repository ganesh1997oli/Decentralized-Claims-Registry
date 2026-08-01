# PostgreSQL claim-intelligence storage

This module implements the claim-processing audit pipeline outside the
blockchain. Sepolia receives the compact status and score; PostgreSQL keeps
versioned feature snapshots, XGBoost results, claim-specific SHAP reasons,
transaction receipts, and processing errors.

It also stores keyed incident fingerprints used to find possible duplicates
across participating synthetic insurers. PostgreSQL never stores the HMAC key,
and the fingerprint is not returned to the browser.

## Versioned feature processing

For each new verified Kafka event, `ClaimFeatureProcessor` writes one
`claim-processing-v1` row to `claim_feature_snapshots` before XGBoost runs. The
row contains the structured model inputs and these derived research features:

- report delay in whole days, from the incident date to the UTC block-event date;
- claim amount divided by policy premium;
- prior claims for the same HMAC-protected insurer/policy identity;
- prior claims for the insurer;
- the insurer's prior average claim amount;
- the current amount divided by that prior average; and
- the number of possible cross-insurer incident matches at processing time.

Historical counts and averages mean **previously processed** claims in the same
chain, contract, and insurer. An advisory transaction lock gives concurrent
claims for one insurer a definite order while allowing unrelated insurers to
continue independently.

The table stores a keyed HMAC of the normalized insurer and policy reference,
not the raw policy reference. It also excludes the claim reference, description,
and evidence. The same server-side secret is used by duplicate detection, but a
separate versioned HMAC payload keeps the two fingerprint purposes isolated.

The proposal's true policy-age and shared-address features are not present
because claim payload schema v4 collects neither a policy start date nor an
address.
Adding either feature requires an intentional claim-schema migration, privacy
review, representative data, and model retraining. The pipeline does not
substitute vehicle age for policy age or invent an address value.

## Idempotency

Every Kafka event has a deterministic `event_id`. PostgreSQL uses it as the
primary key and also allows only one snapshot and assessment for a chain,
contract, and claim. A feature replay returns the original snapshot; it does not
recompute history using claims that arrived later. Completed assessments are
not replaced by a replay.

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

Dashboard assessment and duplicate lookups are always scoped by the selected
`chain_id`, `contract_address`, and `claim_id`. Reusing a numeric claim ID on a
new deployment therefore cannot expose a record from the old contract.

## Local setup

PostgreSQL is included with the Kafka Compose environment:

```bash
docker compose -f integrations/kafka/compose.yml up -d postgres
cp .env.example .env.local
```

The worker creates the feature, assessment, and fingerprint tables and indexes
on startup. Load the connection setting where either FastAPI or the worker needs
access:

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
concurrent duplicate submissions, verifies historical feature aggregation and
replay stability, and then removes only that schema:

```bash
TEST_DATABASE_URL=postgresql://claims:claims-local@127.0.0.1:5432/claims_registry \
  python -m pytest integrations/postgres/tests -m integration -q
```

Inspect saved snapshots in the local research database:

```sql
SELECT
    claim_id,
    feature_version,
    report_delay_days,
    claim_to_premium_ratio,
    prior_policy_claim_count,
    prior_insurer_claim_count,
    prior_insurer_average_claim_amount_usd,
    claim_to_prior_insurer_average_ratio,
    cross_insurer_duplicate_match_count
FROM claim_feature_snapshots
ORDER BY created_at DESC;
```
