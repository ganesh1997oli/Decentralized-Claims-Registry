# Kafka integration

Kafka separates the permanent claim anchor from slower duplicate screening and
model work. Messages contain blockchain and IPFS references—not the full claim.

## Event path

```mermaid
flowchart LR
    Chain["Confirmed ClaimSubmitted"] --> Listener["Listener verifies IPFS hash"]
    Listener --> Topic[("deployment-specific claims topic")]
    Topic --> Worker["Scoring worker"]
    Worker --> Verify["Reverify hash + signed insurer authorization"]
    Verify --> Valid{"Immutable input valid?"}
    Valid -->|Yes| Duplicate["Cross-insurer duplicate check"]
    Valid -->|No| DeadLetter[("Durable dead-letter file")]
    DeadLetter --> RejectedCommit["Commit rejected event offset"]
    Duplicate --> Features["Versioned feature snapshot"]
    Features --> Model["XGBoost + local SHAP"]
    Model --> DB[("PostgreSQL")]
    DB --> Write["Sepolia assessment"]
    Write --> Commit["Commit Kafka offset"]
```

## Delivery guarantees

```mermaid
flowchart TD
    Event["Blockchain log"] --> ID["event_id = chain + tx hash + log index"]
    ID --> Publish{"Kafka acknowledged?"}
    Publish -->|No| SameBlock["Checkpoint stays; publish again"]
    Publish -->|Yes| Handle{"Worker completed?"}
    Handle -->|Temporary failure| SameOffset["Offset stays; handle again"]
    Handle -->|Permanent input defect| Quarantine["fsync public rejection metadata"]
    Quarantine --> Done
    Handle -->|Yes| Done["Commit offset"]
    SameOffset --> Existing{"Existing database / chain state?"}
    Existing -->|Completed| Done
    Existing -->|Partial| Repair["Resume without changing saved score"]
```

This is at-least-once delivery with application-level idempotency. PostgreSQL
uses the deterministic event ID and chain/contract/claim identity as uniqueness
boundaries. A replay after the chain write checks the existing status before
submitting another transaction.

## Permanent claim quarantine

The worker deliberately treats failures in two different ways:

- Temporary infrastructure failures—such as an unavailable IPFS gateway,
  PostgreSQL, RPC endpoint, or Kafka broker—escape the handler. Kafka does not
  commit that offset, so the same event is retried later.
- Permanent defects in the immutable claim—an unsupported/malformed stored
  schema, invalid gateway authorization, or mismatch between the authorized
  insurer wallet and the on-chain claimant—are written to an append-only JSONL
  dead-letter file. The file is flushed and `fsync` is called before the handler
  returns, allowing Kafka to commit the rejected event and process the next
  claim in that partition.

The record contains only public replay coordinates: chain, contract, claim,
block, transaction, log index, IPFS pointer, and a sanitized reason code. It
does not copy IPFS claim bytes, insurer API credentials, authorization
signatures, descriptions, or evidence.

For local runs, the default file is:

```text
packages/integrations/kafka/.state/<deployment-id>-dead-letter.jsonl
```

Set `SCORING_STATE_DIR` to choose another directory or
`SCORING_DEAD_LETTER_FILE` to choose the complete filename. If the file cannot
be written durably, the worker fails closed: the error remains uncommitted and
Kafka retries it. Never delete or edit a dead-letter record merely to reduce
lag; investigate the public event and document any intentional replay first.

## Files

| File | Responsibility |
| --- | --- |
| `events.py` | Versioned schema, configuration, producer and manual-commit consumer |
| `scoring_worker.py` | Complete idempotent screening and write-back handler |
| `consumer.py` | Small verification-only diagnostic consumer |
| `compose.yml` | Local Kafka, PostgreSQL and Kafka UI |
| `tests/` | Schema, adapter, listener bridge and scoring integration tests |

## Start local services

From the repository root:

```bash
docker compose -f packages/integrations/kafka/compose.yml up -d
docker compose -f packages/integrations/kafka/compose.yml ps
```

The initialization service creates the legacy `claims.submitted.v1` topic. The
gasless deployment deliberately uses a new topic name so its event stream cannot
be confused with legacy contract history. After loading `.env.local`, create the
configured topic once:

```bash
set -a; source .env.local; set +a
docker compose -f packages/integrations/kafka/compose.yml exec kafka \
  /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 \
  --create --if-not-exists \
  --topic "$KAFKA_CLAIM_SUBMITTED_TOPIC" \
  --partitions 3 --replication-factor 1
```

Kafka UI is available at <http://127.0.0.1:8081>.

## Configure and run

```bash
cp .env.example .env.local
set -a
source .env.local
set +a

apps/backend/.venv/bin/python \
  -m packages.integrations.postgres.migrations upgrade
apps/backend/.venv/bin/python \
  -m packages.integrations.postgres.migrations check
```

The local example uses:

```dotenv
KAFKA_ENABLED="true"
KAFKA_BOOTSTRAP_SERVERS="127.0.0.1:9092"
KAFKA_CLAIM_SUBMITTED_TOPIC="claims.submitted.sepolia-gasless-v1"
KAFKA_CONSUMER_GROUP_ID="claims-registry-scorer-sepolia-gasless-v1"
KAFKA_SECURITY_PROTOCOL="PLAINTEXT"
```

Start the worker and listener in separate configured terminals:

```bash
apps/backend/.venv/bin/python \
  -m packages.integrations.kafka.scoring_worker
apps/backend/.venv/bin/python -m apps.listener.claims_listener
```

The worker needs the assessor wallet, IPFS gateway, PostgreSQL, model artifact,
`CLAIM_AUTHORIZATION_KEY`, and `DUPLICATE_FINGERPRINT_KEY`. It does not receive
raw insurer API keys, their digests, the submitter wallet, or Pinata JWT.

Do not run `consumer.py` with the scorer's group ID: consumers in one Kafka
group divide partitions and would take messages away from the scoring worker.

## Test

Isolated tests:

```bash
apps/backend/.venv/bin/python -m pytest \
  packages/integrations/kafka/tests -m "not integration" -q
```

Broker- and database-backed tests:

```bash
TEST_DATABASE_URL=postgresql://claims:claims-local@127.0.0.1:5432/claims_registry \
TEST_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092 \
  apps/backend/.venv/bin/python -m pytest -m integration
```

The integration suite exercises a schema round trip, the real listener-to-topic
bridge, scoring persistence, replay recovery, feature history, and concurrent
cross-insurer matching.

## Stop

```bash
docker compose -f packages/integrations/kafka/compose.yml down
```

Add `--volumes` only when you intentionally want to remove local Kafka and
PostgreSQL data.

The local single broker uses plaintext inside the development boundary. A
production design still needs managed or replicated brokers, TLS/SASL,
centralized dead-letter retention, alerting, and an approved replay workflow.

See the [listener guide](../../../apps/listener/README.md) and the
[PostgreSQL guide](../postgres/README.md).
