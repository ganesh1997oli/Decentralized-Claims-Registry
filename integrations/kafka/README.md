# Kafka integration

This module streams verified blockchain claim events through Kafka. Kafka does
not contain the full claim document; each message carries the blockchain and
IPFS references needed by a downstream worker.

## What is included

- `events.py`: versioned event schema, configuration, producer, and consumer
- `consumer.py`: demonstration worker that downloads the IPFS bytes and checks
  their Keccak-256 hash
- `compose.yml`: one-node Kafka environment for local development
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
Kafka consumer
        │ verify the CID and hash again
        ▼
commit Kafka offset
```

The listener advances its block checkpoint only after Kafka acknowledges every
event in the processed range. The consumer commits its offset only after its
handler succeeds. This gives at-least-once delivery: a downstream database must
treat the deterministic `event_id` as a unique idempotency key.

## Install

Use the same Python environment as the listener:

```bash
source backend/.venv/bin/activate
pip install -r listener/requirements.txt
```

Docker Desktop is required for the local broker.

## Start the local broker

From the repository root:

```bash
docker compose -f integrations/kafka/compose.yml up -d
docker compose -f integrations/kafka/compose.yml ps
```

The initialization container creates `claims.submitted.v1` with three partitions
and seven-day retention. Confirm that it exists:

```bash
docker compose -f integrations/kafka/compose.yml exec kafka \
  /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 \
  --describe --topic claims.submitted.v1
```

## Configure Kafka

Create the ignored Kafka environment file from the tracked template:

```bash
cp integrations/kafka/.env.example integrations/kafka/.env.local
```

For the local broker, set the following values in `.env.local`:

```dotenv
KAFKA_ENABLED="true"
KAFKA_BOOTSTRAP_SERVERS="127.0.0.1:9092"
KAFKA_CLAIM_SUBMITTED_TOPIC="claims.submitted.v1"
KAFKA_SECURITY_PROTOCOL="PLAINTEXT"
```

The Kafka consumer also verifies IPFS content, so it loads both integration
environment files:

```bash
set -a
source integrations/ipfs/.env.local
source integrations/kafka/.env.local
set +a
```

Additional variables support the client ID, consumer group, delivery timeout,
poll interval, and TLS/SASL credentials. Their names and safe local defaults are
documented in `integrations/kafka/.env.example`.

## Run the event flow

Start both processes before submitting a new synthetic claim.

Terminal A:

```bash
source backend/.venv/bin/activate
set -a
source integrations/ipfs/.env.local
source integrations/kafka/.env.local
set +a
python -m integrations.kafka.consumer
```

Terminal B:

```bash
source backend/.venv/bin/activate
set -a
source listener/.env.local
source integrations/ipfs/.env.local
source integrations/kafka/.env.local
set +a
python listener/claims_listener.py
```

Submit through the React form or `POST /claims`. A successful flow prints
`KafkaPublished` in the listener and `KafkaProcessed` in the consumer.

## Test

Run the isolated tests without a broker:

```bash
source backend/.venv/bin/activate
python -m pytest integrations/kafka/tests -q
```

Run the opt-in live producer/consumer test after the local broker is healthy:

```bash
KAFKA_INTEGRATION_TEST=true \
  python -m pytest integrations/kafka/tests/test_integration.py -q
```

## Stop the broker

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
