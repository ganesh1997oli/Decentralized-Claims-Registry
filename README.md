# Decentralized Claims Registry

On-chain insurance-claims registry (Solidity / Hardhat 3) with an off-chain
Python listener that feeds claim events to an AI fraud-detection pipeline and
writes verdicts back on-chain.

```
contract/   Solidity contract, Ignition deploy modules, tests (TS + Solidity)
integrations/ Shared IPFS storage and Kafka event modules
listener/   Python blockchain listener and claim demonstration script
backend/    FastAPI: validate, upload and submit synthetic claims (Week 3)
frontend/   React + Tailwind: browser claim-submission form (Week 4 / M1)
model/      Versioned synthetic logistic model + inference reasons (Week 5)
```

## Prerequisites

- Node.js 22+ and npm
- Python 3.10+
- git
- A Pinata account and JWT with public Files write access

## 1. Install and test the contract

```bash
cd contract
npm install
npx hardhat test          # runs Solidity tests + TypeScript tests
```

All tests must pass before anything else. (`npx hardhat compile` also fixes
editor errors like "'claim' is of type 'unknown'" after a fresh clone.)

## 2. Set up the Python listener

```bash
cd listener
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
# Fill in PINATA_JWT and, for Sepolia, the RPC URL and test-wallet key.
# Then load it into each shell that runs a Python script:
set -a; source .env.local; set +a
```

Security note: this repository previously tracked `listener/.env`. Never add a
Pinata JWT to that legacy file. Treat any private key already committed there as
compromised, replace it with a fresh Sepolia-only key, and remove the tracked
file from Git before publishing the repository.

## 3. Full local end-to-end run (three terminals)

Terminal A - local chain:
```bash
cd contract && npx hardhat node
```

Terminal B - deploy, then start the listener:
```bash
cd contract
npx hardhat ignition deploy ignition/modules/Claimsregistry.ts --network localhost

cd ../listener && source .venv/bin/activate
SEPOLIA_RPC_URL=http://127.0.0.1:8545 \
IGNITION_DIR=../contract/ignition/deployments/chain-31337 \
POLL_INTERVAL=1 CONFIRMATION_BLOCKS=0 \
python claims_listener.py
```

Terminal C - submit a claim and write a fraud verdict back:
```bash
cd listener && source .venv/bin/activate
set -a; source .env.local; set +a
SEPOLIA_RPC_URL=http://127.0.0.1:8545 \
IGNITION_DIR=../contract/ignition/deployments/chain-31337 \
SEPOLIA_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 \
python submit_and_assess_demo.py
```

That SEPOLIA_PRIVATE_KEY is Hardhat's publicly known dev account #0 - safe on a
local node, never usable on a real network. The submitter uploads a synthetic
JSON claim to public IPFS before it sends the transaction. Terminal B should
print `[ClaimSubmitted]`, `[IPFSVerified]`, and `[ClaimAssessed]`.

## 4. Sepolia

```bash
cd contract
cp .env.example .env      # fill in a FRESH funded key, then:
set -a; source .env; set +a
npx hardhat ignition deploy ignition/modules/Claimsregistry.ts --network sepolia

cd ../listener && source .venv/bin/activate
cp .env.example .env.local  # first run only; fill in test key + Pinata JWT
set -a; source .env.local; set +a
python claims_listener.py                    # terminal 1: defaults to Sepolia

# In terminal 2, activate/load the same listener environment, then:
python submit_and_assess_demo.py
```

If submission succeeds but assessment is interrupted, resume the existing
claim without uploading or submitting a duplicate:

```bash
python submit_and_assess_demo.py --assess-existing 1  # replace 1 with its ID
```

The listener reads the deployed address and ABI straight from
`contract/ignition/deployments/chain-11155111/`, so there is nothing to copy
by hand after deployment. Commit that directory when it changes - it is what
lets a fresh clone find the contract.

## 5. What the IPFS integration proves

`submit_and_assess_demo.py` creates a canonical synthetic claim JSON document,
uploads the exact bytes to public IPFS through Pinata, downloads them once as a
preflight check, and stores both `ipfs://<CID>` and their Keccak-256 hash in the
claim registry. When `claims_listener.py` receives `ClaimSubmitted`, it fetches
the CID through `IPFS_GATEWAY` and independently compares the downloaded bytes
with the on-chain hash.

This milestone deliberately uses synthetic data and does not encrypt it. Never
upload real names, addresses, photographs, policy documents, or other personal
data to public IPFS.

The Pinata upload, gateway download and pointer-validation implementation is
grouped under [`integrations/ipfs/`](integrations/ipfs/README.md). FastAPI, the
blockchain listener and the Kafka consumer import the same small interface.

## 6. Week 3 FastAPI backend

The `backend/` service turns the existing IPFS and Sepolia submission demo into
an HTTP API suitable for the proposal's later React form. It exposes
`POST /claims` and returns the assigned claim ID and transaction hash.

See [`backend/README.md`](backend/README.md) for installation, tests, environment
configuration and a complete example request.

## 7. Week 4 React form (M1)

The `frontend/` application collects a synthetic claim, calls `POST /claims`,
and displays the confirmed Sepolia transaction and IPFS pointer. The backend
explicitly allows the local Vite origins through CORS; secrets remain in the
backend environment and are never included in the browser bundle.

See [`frontend/README.md`](frontend/README.md) for installation and run commands.

## 8. Week 5 fraud-scoring integration

After a claim is anchored, FastAPI scores the submitted synthetic fields with a
versioned logistic-regression artifact. Claims above the validation-tuned
threshold are written back as `Flagged`; all others remain `UnderReview` so the
model never automatically approves or rejects a claim. The response includes
the model version, probability, compact contributing indicators, and the second
Sepolia transaction receipt.

```bash
python -m model.train
pytest model/tests backend/tests -q
```

The tracked artifact is trained only on deterministic synthetic rows. Its
metrics and explanations must not be represented as evidence of performance on
real insurance data. See [`model/README.md`](model/README.md).

The same frontend includes a paginated claims dashboard backed by
`GET /claims?page=1&page_size=10`. It reads only the requested newest-first
slice of claim IDs from Sepolia and displays status, fraud score, claimant,
IPFS pointer, and timestamps. Page sizes of 5, 10, 25, or 50 are available in
the interface. This is intentionally a small-testnet implementation;
searchable production history belongs in an indexer such as The Graph or
PostgreSQL.

## 9. Kafka event bridge

The listener can now publish every verified `ClaimSubmitted` log to the
versioned `claims.submitted.v1` topic. The message contains the on-chain claim
ID, IPFS pointer and hash, plus the block and transaction identity. It does not
copy the claim document into Kafka.

The flow is:

```text
Sepolia log -> listener -> verify IPFS bytes -> Kafka -> verifier consumer
                         -> save block cursor      -> commit Kafka offset
```

The listener saves its block cursor only after Kafka acknowledges all events in
the block range. The consumer commits its Kafka offset only after it downloads
the IPFS document and verifies its hash. Delivery is therefore at-least-once:
the deterministic `event_id` must be a unique key when the next-stage
PostgreSQL processor is added.

### Run Kafka locally

Install the new Python client in the same virtual environment used for the
listener:

```bash
source backend/.venv/bin/activate
pip install -r listener/requirements.txt
```

Start the single-node development broker and create the topic:

```bash
docker compose -f integrations/kafka/compose.yml up -d
docker compose -f integrations/kafka/compose.yml ps
docker compose -f integrations/kafka/compose.yml exec kafka \
  /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 \
  --describe --topic claims.submitted.v1
```

In `listener/.env.local`, copy the Kafka variables from `.env.example` and set:

```dotenv
KAFKA_ENABLED="true"
KAFKA_BOOTSTRAP_SERVERS="127.0.0.1:9092"
```

Load that file in each new terminal:

```bash
set -a; source listener/.env.local; set +a
```

Then run these alongside the existing backend and frontend:

```bash
# Terminal 1: consume and independently verify Kafka events
python -m integrations.kafka.consumer

# Terminal 2: read confirmed Sepolia logs and publish them
cd listener && python claims_listener.py
```

Submit a new synthetic claim through the React form or `POST /claims`. The
listener should print `[KafkaPublished]` and the consumer should print
`[KafkaProcessed]`. On its first run the listener starts at the latest confirmed
block, so submit the test claim after it starts. To backfill deliberately, stop
the listener, remove its matching file under `listener/.state/`, and set
`LISTENER_START_BLOCK` to the first block you want to read.

Run the listener tests with:

```bash
backend/.venv/bin/python -m pytest \
  listener/test_*.py integrations/ipfs/tests integrations/kafka/tests -q

# Optional real-broker producer/consumer smoke test
KAFKA_INTEGRATION_TEST=true \
  backend/.venv/bin/python -m pytest \
  integrations/kafka/tests/test_integration.py -q
```

The Docker broker is intentionally a one-node, plaintext development service.
A real deployment should use a multi-broker or managed cluster with TLS/SASL,
secret-managed credentials, replication, monitoring and alerting. The client
already accepts `SASL_SSL` settings, but those credentials must never be
committed to Git.

The Kafka implementation and its local infrastructure are grouped under
[`integrations/kafka/`](integrations/kafka/README.md).
