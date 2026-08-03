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

The shared IPFS and Kafka code lives under `packages/integrations/` rather than inside the
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

When a restart resumes from a stale checkpoint, the listener drains consecutive
bounded ranges without applying the live polling delay between them. It waits
for `POLL_INTERVAL` only after reaching the current confirmed chain head. This
keeps each RPC query small without making a new claim wait behind a slow
historical backfill.

If RPC, IPFS, or Kafka processing fails, the checkpoint does not advance and the
same range is retried. A structurally invalid immutable pointer or a permanent
hash mismatch is instead written to the durable dead-letter JSONL file and
skipped, so one bad historical event cannot halt every later claim. Review that
file before deciding whether to replay or investigate an event.

## Install

You can reuse the backend environment:

```bash
source apps/backend/.venv/bin/activate
pip install -r apps/listener/requirements.txt
```

Alternatively, create a dedicated environment:

```bash
python3 -m venv apps/listener/.venv
source apps/listener/.venv/bin/activate
pip install -r apps/listener/requirements.txt
```

## Configure

Create the shared local file from the repository root:

```bash
cp .env.example .env.local
```

Add your local values, then load the same file used by the rest of the
application:

```bash
set -a
source .env.local
set +a
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `SEPOLIA_RPC_URL` | Public Sepolia endpoint in the script | Ethereum RPC endpoint |
| `SEPOLIA_SUBMITTER_PRIVATE_KEY` | Empty | Used only by the command-line submission demo |
| `SEPOLIA_ASSESSOR_PRIVATE_KEY` | Empty | Used only by the command-line assessment demo |
| `CLAIMS_DEPLOYMENT_ID` | Required | Checked-in deployment directory; use `sepolia-security-audit-v1` for the hardened contract |
| `POLL_INTERVAL` | `5` | Seconds between polling attempts |
| `CONFIRMATION_BLOCKS` | `2` | Blocks held back for basic reorganization safety |
| `MAX_BLOCK_RANGE` | `50` | Maximum logs query size while catching up on a public RPC |
| `LISTENER_STATE_DIR` | `apps/listener/.state` | Directory for deployment-specific checkpoints and dead-letter files |
| `LISTENER_START_BLOCK` | Latest confirmed block | First block for a deliberate initial backfill |
| `CLAIM_AUTHORIZATION_KEY` | Required by demo | HMAC key used to create a worker-verifiable schema-v4 claim |
| `DEMO_INSURER_CREDENTIAL_ID` | `northstar-local-v1` | Public credential label embedded by the direct submission demo |

Checkpoint and dead-letter filenames automatically include the deployment ID,
chain ID, and contract address, so changing the selector starts a separate state
namespace inside `LISTENER_STATE_DIR`.

IPFS variables are documented in the
[IPFS guide](../../packages/integrations/ipfs/README.md). Kafka variables are documented in
the [Kafka guide](../../packages/integrations/kafka/README.md).

The listener only downloads IPFS data, so it does not require `PINATA_JWT`,
`CLAIM_AUTHORIZATION_KEY`, or any wallet key. The two role keys and claim
authorization key are needed only by `submit_and_assess_demo.py`.

## Run

From the repository root:

```bash
source apps/backend/.venv/bin/activate
set -a
source .env.local
set +a
python -m apps.listener.claims_listener
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
Rejected immutable events print `[ClaimQuarantined]` and are appended to the
dead-letter file.

## First run and backfilling

On its first run, the listener starts at the latest confirmed block. Start it
before submitting a new test claim if you only want live events.

To read older events deliberately:

1. stop the listener;
2. identify the deployment-specific checkpoint under `LISTENER_STATE_DIR`;
3. remove that checkpoint;
4. set `LISTENER_START_BLOCK` to the required historical block;
5. restart the listener.

The checked-in hardened deployment was created at Sepolia block `11377814`; use
that value for `LISTENER_START_BLOCK` when intentionally rebuilding its complete
event history. A different deployment must use its own deployment block and its
own checkpoint/dead-letter filenames.

At-least-once processing means a retried event can appear more than once. Kafka
and database consumers should use the event ID for deduplication.

## Command-line demonstration

The backend is the recommended submission path. For a smaller trusted
terminal-only demonstration, load a Pinata JWT, `CLAIM_AUTHORIZATION_KEY`, and
the separately authorized submitter and assessor keys, then run:

```bash
set -a
source .env.local
set +a
python -m apps.listener.submit_and_assess_demo
```

The script signs the schema-v4 IPFS document with the same authorization key as
the worker. It is therefore a trusted operator tool, not an untrusted insurer
client, and the authorization key must never be copied into browser code.

If submission succeeded but assessment was interrupted, continue the existing
claim instead of creating another one:

```bash
python -m apps.listener.submit_and_assess_demo --assess-existing 1
```

Replace `1` with the actual claim ID.

## Test

```bash
source apps/backend/.venv/bin/activate
python -m pytest apps/listener/test_*.py -q
```

These tests exercise confirmed-block handling, event ordering, IPFS tamper
rejection, Kafka publication and durable-checkpoint safety through injected
adapters. They do not connect to Sepolia, IPFS or Kafka.

See the [root project guide](../../README.md) for the complete application run.
