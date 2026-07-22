# Blockchain listener

The listener watches the deployed `ClaimsRegistry` contract for new submissions
and assessments. It independently verifies every submitted IPFS document against
the hash stored on-chain and can publish the verified event to Kafka.

It is useful for observing the complete claim lifecycle without depending on the
browser or backend response.

## What is included

- `claims_listener.py` polls `ClaimSubmitted` and `ClaimAssessed` logs.
- `block_cursor.py` stores the last fully processed block safely on disk.
- `submit_and_assess_demo.py` runs the older command-line IPFS and contract
  demonstration without the web application.
- `test_block_cursor.py` and `test_submit_nonce.py` cover checkpoint and nonce
  behaviour.

The shared IPFS and Kafka code lives under `integrations/` rather than inside the
listener.

## Processing behaviour

The listener reads logs in confirmed block ranges and handles them in blockchain
order. For each `ClaimSubmitted` event it:

1. prints the on-chain claim reference;
2. downloads the `ipfs://` document through the configured gateway;
3. calculates Keccak-256 over the exact returned bytes;
4. compares that hash with the value stored in the event;
5. optionally publishes a versioned Kafka message;
6. advances the local block checkpoint only after processing succeeds.

If RPC, IPFS, or Kafka processing fails, the checkpoint does not advance and the
same range is retried.

## Install

You can reuse the backend environment:

```bash
source backend/.venv/bin/activate
pip install -r listener/requirements.txt
```

Alternatively, create a dedicated environment:

```bash
python3 -m venv listener/.venv
source listener/.venv/bin/activate
pip install -r listener/requirements.txt
```

## Configure

```bash
cp listener/.env.example listener/.env.local
```

Load it before starting the listener:

```bash
set -a; source listener/.env.local; set +a
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `SEPOLIA_RPC_URL` | Public Sepolia endpoint in the script | Ethereum RPC endpoint |
| `IPFS_GATEWAY` | Pinata public gateway | Downloads and verifies the claim document |
| `IGNITION_DIR` | Sepolia Ignition deployment | Address and ABI location |
| `MODULE_ID` | `ClaimsRegistryModule#ClaimsRegistry` | Ignition artifact ID |
| `POLL_INTERVAL` | `5` | Seconds between polling attempts |
| `CONFIRMATION_BLOCKS` | `2` | Blocks held back for basic reorganization safety |
| `LISTENER_STATE_FILE` | File under `listener/.state/` | Durable block checkpoint |
| `LISTENER_START_BLOCK` | Latest confirmed block | First block for a deliberate initial backfill |
| `KAFKA_ENABLED` | `false` | Publishes verified submissions when enabled |

Kafka connection and security variables are listed in `.env.example` and
explained in the [Kafka guide](../integrations/kafka/README.md).

The listener only downloads IPFS data, so it does not require `PINATA_JWT`.
`SEPOLIA_PRIVATE_KEY` is needed only by `submit_and_assess_demo.py`.

## Run

From the repository root:

```bash
source backend/.venv/bin/activate
set -a; source listener/.env.local; set +a
python listener/claims_listener.py
```

Direct execution from the listener directory also remains supported:

```bash
cd listener
python claims_listener.py
```

Expected output for a complete claim includes:

```text
[ClaimSubmitted] ...
[IPFSVerified] ...
[ClaimAssessed] ...
```

With Kafka enabled, a verified submission also prints `[KafkaPublished]`.

## First run and backfilling

On its first run, the listener starts at the latest confirmed block. Start it
before submitting a new test claim if you only want live events.

To read older events deliberately:

1. stop the listener;
2. identify the matching checkpoint under `listener/.state/`;
3. remove that checkpoint;
4. set `LISTENER_START_BLOCK` to the required historical block;
5. restart the listener.

At-least-once processing means a retried event can appear more than once. Kafka
and database consumers should use the event ID for deduplication.

## Command-line demonstration

The backend is the recommended submission path. For a smaller terminal-only
demonstration, load a Pinata JWT and a funded assessor key, then run:

```bash
python listener/submit_and_assess_demo.py
```

If submission succeeded but assessment was interrupted, continue the existing
claim instead of creating another one:

```bash
python listener/submit_and_assess_demo.py --assess-existing 1
```

Replace `1` with the actual claim ID.

## Test

```bash
source backend/.venv/bin/activate
python -m pytest listener/test_*.py -q
```

These tests do not connect to Sepolia, IPFS, or Kafka.

See the [root project guide](../README.md) for the complete application run.
