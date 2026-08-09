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
    Verify --> Duplicate["Cross-insurer duplicate check"]
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
    Handle -->|No| SameOffset["Offset stays; handle again"]
    Handle -->|Yes| Done["Commit offset"]
    SameOffset --> Existing{"Existing database / chain state?"}
    Existing -->|Completed| Done
    Existing -->|Partial| Repair["Resume without changing saved score"]
```

This is at-least-once delivery with application-level idempotency. PostgreSQL
uses the deterministic event ID and chain/contract/claim identity as uniqueness
boundaries. A replay after the chain write checks the existing status before
submitting another transaction.

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
production design needs managed or replicated brokers, TLS/SASL, monitoring,
retry policy, and a dead-letter workflow.

See the [listener guide](../../../apps/listener/README.md) and the
[PostgreSQL guide](../postgres/README.md).
