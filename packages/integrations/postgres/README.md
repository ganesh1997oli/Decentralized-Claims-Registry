# PostgreSQL claim-processing storage

Sepolia remains authoritative. PostgreSQL keeps a rebuildable public claim
projection for fast API reads plus the richer, versioned records needed to
explain and safely replay off-chain screening.

## Stored records

```mermaid
erDiagram
    INDEXED_CLAIMS {
        bigint chain_id PK
        text contract_address PK
        bigint claim_id PK
        smallint status
        int fraud_score
        bigint state_block_number
    }
    CLAIM_INDEX_EVENTS {
        text event_id PK
        bigint block_number
        int log_index
        text event_type
    }
    CLAIM_INDEX_CHECKPOINTS {
        bigint chain_id PK
        text contract_address PK
        bigint last_processed_block
    }
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
    CLAIM_ASSESSOR_OUTCOMES {
        uuid outcome_id PK
        bigint chain_id
        text contract_address
        bigint claim_id
        int revision
        text outcome
        text assessor_reference
        text notes
        timestamptz assessed_at
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
    GASLESS_CLAIM_SUBMISSIONS {
        uuid submission_id PK
        text credential_id
        text signer_address
        text state
        numeric forwarder_nonce
        numeric relayer_nonce
        text transaction_hash
    }
    GASLESS_RELAYER_NONCES {
        bigint chain_id PK
        text relayer_address PK
        numeric next_nonce
    }
    GASLESS_RELAY_ATTEMPTS {
        uuid submission_id PK
        int attempt_number PK
        text transaction_hash
        numeric relayer_nonce
    }
    GASLESS_CLAIM_SUBMISSIONS ||--o{ GASLESS_RELAY_ATTEMPTS : retains
```

Every lookup is scoped by chain ID, contract address, and claim ID. A claim
number reused by a new deployment cannot expose the old contract's result.

## Module map

| File | Owns |
| --- | --- |
| `database.py` | Connection configuration and one-transaction cursor lifetime |
| `claim_index_repository.py` | Idempotent event projection, indexed pages, and database checkpoint |
| `gasless_submission_repository.py` | Idempotency, durable quotas, outbox transitions, EOA nonce reservation and relay attempts |
| `assessment_repository.py` | Score, SHAP, processing state and chain receipt |
| `assessor_outcome_repository.py` | Append-only human fraud conclusions and correction revisions |
| `duplicate_repository.py` | Private incident fingerprint and current matches |
| `feature_processor.py` | Validation, direct features and policy HMAC |
| `feature_repository.py` | Historical enrichment and immutable feature snapshots |
| `repositories.py` | Small composition root used by API and worker |
| `migrations/` | Ordered, checksummed schema changes |

Runtime callers receive focused repositories, not a general SQL cursor or
permission to create schema.

## Human assessor outcomes

`claim_assessor_outcomes` is intentionally independent from
`claim_assessments`. The latter is immutable model evidence; the former is an
attributable human conclusion selected from `ConfirmedFraud`, `Legitimate`, and
`Inconclusive`. An advisory lock allocates monotonic revisions per
chain/contract/claim, preserving corrections without silently replacing prior
review evidence.

The table stores neither `Approved` nor `Rejected`, because those contract states
are business dispositions rather than fraud labels. It also contains no model
version, probability, retraining flag, or deployment action. `Inconclusive`
records remain useful audit evidence but are not eligible for a binary label.

## Gasless submission outbox

The gasless workflow must survive browser retries, API restarts, relayer
restarts, and the uncertain period between broadcasting a transaction and
receiving its receipt. PostgreSQL is therefore part of the transaction protocol,
not merely an analytics store.

`gasless_claim_submissions` is both the insurer-visible state machine and the
relayer outbox. It stores HMAC fingerprints rather than raw idempotency keys or
client addresses. Reusing an idempotency key with identical claim content
returns the original record; using it with different content is a conflict.

Preparation takes transaction-scoped advisory locks for the credential and
forwarder signer. This makes minute/daily sponsorship counts and the “one active
forwarder nonce per insurer signer” rule consistent across multiple API
processes. A ten-minute preparation lease releases a process that died during
IPFS work. Unsigned prepared requests expire at their EIP-712 deadline.

The relayer separately locks `(chain_id, relayer_address)`, chooses the greater
of PostgreSQL's next nonce and the RPC pending nonce, signs the EOA transaction,
and commits its raw bytes and hash before broadcasting. If the process dies
after the commit, the next run sends the same bytes instead of allocating a new
nonce.

`gasless_relay_attempts` retains the original and every same-nonce fee-bumped
replacement. Confirmation checks every stored hash because either transaction
can win the race into a block. `gasless_relayer_nonces` is an allocator, not an
independent claim of chain truth; unknown external use produces an operational
nonce conflict that must be reconciled rather than silently skipped.

## Blockchain claims index

`ClaimSubmitted` appends an immutable `claim_index_events` row and creates the
corresponding `indexed_claims` projection. `ClaimAssessed` appends another audit
row and advances status, score, and timestamps. The update compares block number
and log index, so replaying an older range cannot overwrite a later state.

The listener persists `claim_index_checkpoints` only after the complete confirmed
range has also passed IPFS verification and Kafka publication. A failure leaves
the checkpoint unchanged; retrying is safe because event IDs and claim keys are
unique. API pagination uses the deployment-scoped, newest-first database index
and includes `indexed_through_block` in its response.

Authenticated event search reads the immutable `claim_index_events` audit table
with optional claim, transaction, event type, state, and block-range filters.
It uses a `(block_number, log_index, event_id)` keyset cursor rather than OFFSET,
so concurrent listener inserts cannot duplicate or skip rows between pages.
Migration `004_claim_index_event_search.sql` adds the focused type, state, and
transaction indexes used by those operator queries.

This projection stores only public contract-event values. It does not download
claim bodies into PostgreSQL. Use
`apps/backend/.venv/bin/python -m apps.listener.reconcile_claim_index`
while the caught-up listener is stopped to compare it with contract state. The
command never mutates indexed claim rows; it appends its compact result to
`claim_index_reconciliations` for the authenticated operations dashboard.

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

test -f .env.local || cp .env.example .env.local
set -a
source .env.local
set +a

apps/backend/.venv/bin/python \
  -m packages.integrations.postgres.migrations upgrade
apps/backend/.venv/bin/python \
  -m packages.integrations.postgres.migrations check
```

`upgrade` takes a PostgreSQL advisory lock and applies each pending file in a
transaction. `check` fails when history is pending, missing, unknown, or edited.
Add a new numbered migration after a shared deployment; never rewrite an applied
migration.

## Test

Isolated repository tests:

```bash
apps/backend/.venv/bin/python -m pytest \
  packages/integrations/postgres/tests -m "not integration" -q
```

Disposable-schema integration tests:

```bash
TEST_DATABASE_URL=postgresql://claims:claims-local@127.0.0.1:5432/claims_registry \
  apps/backend/.venv/bin/python -m pytest \
    packages/integrations/postgres/tests -m integration -q
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
[local development guide](../../../LOCAL_DEVELOPMENT.md) for startup order.
