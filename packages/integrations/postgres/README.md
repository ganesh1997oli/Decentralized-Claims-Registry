# PostgreSQL claim-processing storage

Sepolia keeps the compact public result. PostgreSQL keeps the richer, versioned
record needed to explain and safely replay off-chain screening.

## Stored records

```mermaid
erDiagram
    CLAIM_ASSESSMENTS {
        text event_id PK
        bigint chain_id
        text contract_address
        bigint claim_id
        text model_version
        float probability
        jsonb reasons
        text processing_status
    }
    CLAIM_FEATURE_SNAPSHOTS {
        text event_id PK
        bigint chain_id
        text contract_address
        bigint claim_id
        text feature_version
        text policy_reference_fingerprint
        int prior_insurer_claim_count
    }
    CLAIM_INCIDENT_FINGERPRINTS {
        bigint chain_id PK
        text contract_address PK
        bigint claim_id PK
        text insurer_id
        text fingerprint_version
        text incident_fingerprint
    }
```

Every lookup is scoped by chain ID, contract address, and claim ID. A claim
number reused by a new deployment cannot expose the old contract's result.

## Module map

| File | Owns |
| --- | --- |
| `database.py` | Connection configuration and one-transaction cursor lifetime |
| `assessment_repository.py` | Score, SHAP, processing state and chain receipt |
| `duplicate_repository.py` | Private incident fingerprint and current matches |
| `feature_processor.py` | Validation, direct features and policy HMAC |
| `feature_repository.py` | Historical enrichment and immutable feature snapshots |
| `repositories.py` | Small composition root used by API and worker |
| `migrations/` | Ordered, checksummed schema changes |

Runtime callers receive focused repositories, not a general SQL cursor or
permission to create schema.

## Feature snapshot

```mermaid
flowchart LR
    Claim["Verified claim + event"] --> Direct["Direct structured fields"]
    Claim --> Derived["report delay + amount / premium"]
    Claim --> Policy["HMAC policy identity"]
    History[("Prior snapshots for same insurer")] --> Aggregate["counts + prior average"]
    Direct --> Snapshot["claim-processing-v1 snapshot"]
    Derived --> Snapshot
    Policy --> Snapshot
    Aggregate --> Snapshot
    Duplicate["Cross-insurer match count"] --> Snapshot
```

One advisory transaction lock serializes claims for the same insurer, giving
historical counts a definite order while other insurers continue independently.
On replay, the original snapshot is returned instead of recomputing history with
claims that arrived later.

Stored derived values include:

- report delay in whole days;
- claim-to-premium ratio;
- previous claims for the HMAC-protected policy identity;
- previous claims and prior average amount for the insurer;
- current amount divided by that prior average; and
- cross-insurer match count at processing time.

Raw policy reference, description, evidence, and the HMAC key are not stored.
Policy-age and shared-address features are not invented because schema v4 does
not collect a policy start date or address.

## Duplicate matching

The worker creates a versioned HMAC from normalized incident fields and records
it under the current chain and contract. Matches must have the same fingerprint
and a different insurer ID. Equal fingerprints share an advisory lock so
concurrent submissions cannot pass one another unnoticed.

FastAPI rebuilds the match list when it reads a claim. An earlier claim can
therefore show a later match. The result remains a human-review candidate and
does not alter the model probability.

## Assessment replay

```mermaid
stateDiagram-v2
    [*] --> Scored: save probability, threshold and reasons
    Scored --> Completed: Sepolia write confirmed
    Scored --> Failed: write or dependency failure
    Failed --> Scored: safe replay
    Completed --> Completed: replay is a no-op
```

A worker restart after the Sepolia write reads current chain state. If status
and score already match, it completes the database record without sending a
second transaction. A completed score is never silently replaced.

## Local setup

```bash
docker compose -f packages/integrations/kafka/compose.yml up -d postgres

cp .env.example .env.local
set -a
source .env.local
set +a

python -m packages.integrations.postgres.migrations upgrade
python -m packages.integrations.postgres.migrations check
```

`upgrade` takes a PostgreSQL advisory lock and applies each pending file in a
transaction. `check` fails when history is pending, missing, unknown, or edited.
Add a new numbered migration after a shared deployment; never rewrite an applied
migration.

## Test

Isolated repository tests:

```bash
source apps/backend/.venv/bin/activate
python -m pytest packages/integrations/postgres/tests -m "not integration" -q
```

Disposable-schema integration tests:

```bash
TEST_DATABASE_URL=postgresql://claims:claims-local@127.0.0.1:5432/claims_registry \
  python -m pytest packages/integrations/postgres/tests -m integration -q
```

Inspect recent feature snapshots locally:

```sql
SELECT claim_id,
       feature_version,
       report_delay_days,
       claim_to_premium_ratio,
       prior_policy_claim_count,
       prior_insurer_claim_count,
       cross_insurer_duplicate_match_count
FROM claim_feature_snapshots
ORDER BY created_at DESC;
```

See the [Kafka guide](../kafka/README.md) for replay order and the
[root data map](../../../README.md#what-is-stored-where).
