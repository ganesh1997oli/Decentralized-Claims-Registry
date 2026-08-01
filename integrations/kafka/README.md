# Kafka integration

This module streams verified blockchain claim events through Kafka. Kafka does
not contain the full claim document; each message carries the blockchain and
IPFS references needed by a downstream worker.

## What is included

- `events.py`: versioned event schema, configuration, producer, and consumer
- `consumer.py`: demonstration worker that downloads the IPFS bytes and checks
  their Keccak-256 hash
- `scoring_worker.py`: idempotent duplicate detection, XGBoost, SHAP,
  PostgreSQL and Sepolia workflow
- `compose.yml`: Kafka, PostgreSQL and a Kafka dashboard for local development
- `tests/`: isolated adapter tests and an optional live-broker smoke test

The listener imports the public interface from `integrations.kafka`, keeping
broker configuration and message encoding out of the blockchain polling code.

## Event flow

```text
Sepolia ClaimSubmitted
        │
        ▼
blockchain listener
        │ verify CID bytes against the on-chain hash
        ▼
claims.submitted.v1
        │
        ▼
XGBoost scoring worker
        │ verify CID and signed claim schema v4 authorization
        ▼
private incident HMAC + cross-insurer lookup
        │
        ▼
XGBoost probability + local SHAP
        │
        ▼
PostgreSQL assessment
        │
        ▼
Sepolia assessClaim
        ▼
commit Kafka offset
```

The listener advances its block checkpoint only after Kafka acknowledges every
event. The worker commits its offset only after PostgreSQL and Sepolia agree.
PostgreSQL treats the deterministic `event_id` as a unique idempotency key. A
chain read also makes a replay safe if the process stopped after the transaction
but before updating PostgreSQL.

## Install

Use the same Python environment as the listener:

```bash
source backend/.venv/bin/activate
pip install -r listener/requirements.txt -r model/requirements.txt \
  -r backend/requirements.txt
```

Docker Desktop is required for local Kafka and PostgreSQL.

## Start the local broker

From the repository root:

```bash
docker compose -f integrations/kafka/compose.yml up -d
docker compose -f integrations/kafka/compose.yml ps
```

## View Kafka in the browser

Open `http://127.0.0.1:8081` after the Compose services have started. The
dashboard connects to the local cluster as `claims-local`.

To inspect claim events:

1. Open **Topics**.
2. Select `claims.submitted.v1`.
3. Open the **Messages** tab.
4. Select all partitions and load the messages.

The dashboard also shows partitions, offsets, consumer groups, and consumer
lag. A lag of zero for `claims-registry-scorer-v1` means that the scoring worker
has processed every available event.

Kafka stores the events in the `claims-kafka-data` Docker volume. The dashboard
only reads and displays those events; it does not create a second copy.

The initialization container creates `claims.submitted.v1` with three partitions
and seven-day retention. Confirm that it exists:

```bash
docker compose -f integrations/kafka/compose.yml exec kafka \
  /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 \
  --describe --topic claims.submitted.v1
```

## Configure Kafka

Create the one shared local environment file from the repository root:

```bash
cp .env.example .env.local
```

The example already contains the settings used by the local broker:

```dotenv
KAFKA_ENABLED="true"
KAFKA_BOOTSTRAP_SERVERS="127.0.0.1:9092"
KAFKA_CLAIM_SUBMITTED_TOPIC="claims.submitted.v1"
KAFKA_SECURITY_PROTOCOL="PLAINTEXT"
```

Load the root file before starting the scoring worker:

```bash
set -a
source .env.local
set +a
```

Additional variables support the client ID, consumer group, delivery timeout,
poll interval, and TLS/SASL credentials. Their names and safe local defaults are
documented in the root `.env.example`.

## Run the event flow

Start both processes before submitting a new fictional test claim.

Terminal A, scoring worker:

```bash
source backend/.venv/bin/activate
set -a
source .env.local
set +a
python -m integrations.kafka.scoring_worker
```

Terminal B:

```bash
source backend/.venv/bin/activate
set -a
source .env.local
set +a
python listener/claims_listener.py
```

Submit through the React form or authenticated `POST /claims`. The worker
verifies both the document hash and the gateway HMAC authorization before it
trusts `insurerId`, performs duplicate screening, saves a versioned PostgreSQL
feature snapshot, runs XGBoost/SHAP, and writes the assessment. It receives
`CLAIM_AUTHORIZATION_KEY`, but it does not receive raw insurer API keys or their
digests. A successful flow prints `KafkaPublished` in the listener and
`ClaimAssessed` in the worker; the latter includes
`features=claim-processing-v1`.

The older verification-only consumer remains useful for inspecting events. Do
not run it with the scorer's consumer-group ID because members of the same Kafka
group divide messages between themselves.

## Test

Run the isolated tests without a broker:

```bash
source backend/.venv/bin/activate
python -m pytest integrations/kafka/tests integrations/postgres/tests -q
```

Run all broker- and database-backed integration tests after the local services
are healthy:

```bash
TEST_DATABASE_URL=postgresql://claims:claims-local@127.0.0.1:5432/claims_registry \
TEST_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092 \
  python -m pytest -m integration
```

The integration suite covers three distinct boundaries:

- a Kafka producer/consumer schema round trip;
- the Week 5 bridge from a simulated confirmed blockchain log, through the real
  `ClaimEventProcessor`, into a real Kafka topic; and
- the downstream Kafka/PostgreSQL scoring and cross-insurer matching workflow.

The listener bridge keeps Sepolia and IPFS deterministic so CI does not depend
on public services, but it uses the production listener processor, publisher,
message schema and consumer unchanged.

## Stop local infrastructure

```bash
docker compose -f integrations/kafka/compose.yml down
```

Add `--volumes` only when you deliberately want to delete the local Kafka data.

## Production considerations

The Compose file is a development environment, not a production cluster. A real
deployment needs a managed or multi-broker setup, TLS/SASL, secret-managed
credentials, replication, monitoring, alerting, retry handling, a dead-letter
strategy, and idempotent persistence of processed events.

See the [listener guide](../../listener/README.md) for checkpoint behaviour and
the [root project guide](../../README.md) for the complete application flow.
