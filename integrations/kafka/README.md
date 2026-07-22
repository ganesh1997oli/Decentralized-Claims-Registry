# Kafka claim-event integration

Everything specific to Kafka lives in this module:

- `events.py` owns the versioned message, configuration, producer and consumer.
- `consumer.py` verifies the IPFS bytes before committing a Kafka offset.
- `compose.yml` starts the local one-node broker and creates the topic.
- `tests/` contains isolated client tests and the optional live-broker test.

The blockchain listener imports only the public names from
`integrations.kafka`.
This keeps broker credentials, delivery settings and message encoding out of
`claims_listener.py`.

From the repository root, start the broker with:

```bash
docker compose -f integrations/kafka/compose.yml up -d
```

After loading `listener/.env.local`, start the consumer from the repository
root with:

```bash
python -m integrations.kafka.consumer
```

This Compose setup is for local development. Use a replicated or managed Kafka
cluster with TLS/SASL and secret-managed credentials for deployment.
