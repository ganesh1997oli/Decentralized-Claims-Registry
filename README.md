# Decentralized Claims Registry

A dissertation prototype that explores one practical question: can an insurance
claim be stored off-chain, screened by a machine-learning workflow, and still
leave a small public record that another party can verify independently?

The application combines a Solidity registry, public IPFS storage, FastAPI,
React, Kafka, PostgreSQL, XGBoost, and SHAP. Each part has one clear job, and the
guides explain both what it does and why it is present.

> **Research test data only:** use fictional claim references, values, and
> descriptions. The current IPFS upload is public and unencrypted, the wallet
> holds Sepolia test ETH, and the model was trained on a synthetic research
> dataset. Do not enter real names, addresses, policies, photographs, or files.

## What this prototype demonstrates

- The full claim document does not need to be stored on Ethereum.
- A Keccak-256 hash can prove whether later IPFS bytes match the submitted claim.
- Kafka can separate user submission from slower model processing.
- PostgreSQL can retain detailed model and SHAP context that is unsuitable for a
  smart contract.
- Sepolia can hold a compact, independently visible lifecycle record.

It does **not** demonstrate a production fraud decision. The model supports
research and integration testing; it never approves or rejects a real claim.

## What the application does

When a user submits a fictional test claim:

1. FastAPI validates the form and creates deterministic JSON bytes.
2. The backend uploads the exact JSON bytes to IPFS through Pinata and downloads
   them again to verify the upload.
3. The backend stores the IPFS pointer and the document's Keccak-256 hash in the
   `ClaimsRegistry` contract on Ethereum Sepolia.
4. The listener verifies the claim and publishes its deterministic event to
   Kafka.
5. The scoring worker verifies the IPFS bytes again, runs XGBoost, creates
   claim-specific SHAP reasons, and saves the result in PostgreSQL.
6. The worker writes `UnderReview` or `Flagged` and the score to Sepolia.
7. The React interface polls FastAPI for the assessment and displays the score,
   reasons, transaction, and paginated contract state.

The recommended mode is `CLAIM_SCORING_MODE="async_xgboost"`. The older
`inline_demo` mode remains available for comparing the original synchronous
logistic-regression demonstration without Kafka.

```text
React ──► FastAPI ──► IPFS + Sepolia claim anchor
                           │
                           ▼
                 listener ──► Kafka
                                │
                                ▼
                         XGBoost + SHAP
                                │
                         PostgreSQL audit
                                │
                                ▼
                    Sepolia assessment write-back
```

## Project structure

| Directory | Responsibility | Documentation |
| --- | --- | --- |
| `contract/` | Solidity contract, tests and Ignition deployments | [Contract guide](contract/README.md) |
| `backend/` | FastAPI validation, scoring, IPFS and Sepolia workflow | [Backend guide](backend/README.md) |
| `frontend/` | React claim form, receipt and claims dashboard | [Frontend guide](frontend/README.md) |
| `model/` | Demonstration scoring plus XGBoost/SHAP research evaluation | [Model guide](model/README.md) |
| `listener/` | Blockchain event polling, verification and checkpoints | [Listener guide](listener/README.md) |
| `integrations/ipfs/` | Shared Pinata and IPFS adapter | [IPFS guide](integrations/ipfs/README.md) |
| `integrations/kafka/` | Kafka messages, producer, consumer and local broker | [Kafka guide](integrations/kafka/README.md) |
| `integrations/postgres/` | Idempotent assessment and explanation storage | [PostgreSQL guide](integrations/postgres/README.md) |

## Current Sepolia deployment

- Network: Ethereum Sepolia (`11155111`)
- Contract: `0x57E3203b9427BE41c753bEedD526D81a66bFc2AB`
- Ignition module: `ClaimsRegistryModule#ClaimsRegistry`
- Explorer: [view the contract on Sepolia Etherscan](https://sepolia.etherscan.io/address/0x57E3203b9427BE41c753bEedD526D81a66bFc2AB)

The application reads the address and ABI from
`contract/ignition/deployments/chain-11155111/`; the address is not duplicated in
the Python source.

## Prerequisites

- Node.js 22 or later and npm
- Python 3.10 or later
- A Sepolia RPC endpoint
- A fresh Sepolia-only wallet with test ETH
- A Pinata JWT with public file-upload permission
- Docker Desktop for the local Kafka and PostgreSQL environment

Never use a wallet that holds real assets. The setup below creates one ignored
`.env.local` file for local development. A deployed environment should inject
the same settings through its secret manager instead of copying that file.

## First-time local setup

The Sepolia contract is already deployed, so normal application testing does
not require another contract deployment.

Run the following commands from the repository root.

### 1. Create the Python environment

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  -r backend/requirements.txt \
  -r listener/requirements.txt \
  -r model/requirements.txt \
  -r integrations/kafka/requirements.txt
```

The application shares this environment across FastAPI, the listener, the
worker, and the research model. On macOS, XGBoost also needs OpenMP:

```bash
brew install libomp
```

### 2. Install the frontend

```bash
npm --prefix frontend ci
```

`npm ci` uses the committed lock file, which makes a clean installation more
repeatable than silently updating package versions.

### 3. Configure local secrets

```bash
cp .env.example .env.local
```

Open `.env.local` and add:

- `SEPOLIA_PRIVATE_KEY`: a fresh testnet-only wallet key;
- `PINATA_JWT`: a server-side Pinata upload token.

Keep the remaining defaults unless your local ports differ. `.env.local` is
ignored by Git. If either secret has appeared in a screenshot, chat, or commit,
rotate it rather than continuing to use it.

### 4. Prepare the XGBoost artifact

On a fresh clone, create the reviewed artifact with:

```bash
source backend/.venv/bin/activate
python -m model.train_xgboost --download
```

This command downloads the pinned Hugging Face dataset revision, verifies its
SHA-256 digest, trains the pipeline, and writes the model, metadata, and SHAP
summary under `model/artifacts/xgboost-african-motor-v1/`.

Confirm that the application can load it:

```bash
set -a
source .env.local
set +a
python -c "from model.xgboost_scorer import XGBoostFraudScorer; m=XGBoostFraudScorer.from_env(); print('Model loaded:', m.model_version); print('Threshold:', m.threshold)"
```

The expected model is `african-motor-xgboost-v1`. The current reviewed threshold
is `0.47`, meaning 47%.

## Start the complete application

The asynchronous workflow has four long-running processes plus Docker. Using a
separate terminal for each process makes failures much easier to identify.

### 1. Start Kafka and PostgreSQL

Start Docker Desktop, then run:

```bash
docker compose -f integrations/kafka/compose.yml up -d
docker compose -f integrations/kafka/compose.yml ps
```

Wait until Kafka and PostgreSQL report healthy. The optional Kafka dashboard is
available at <http://127.0.0.1:8081>.

### 2. Start FastAPI — terminal A

```bash
source backend/.venv/bin/activate
set -a
source .env.local
set +a
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Confirm the process at <http://127.0.0.1:8000/health>. The interactive API
documentation is at <http://127.0.0.1:8000/docs>.

### 3. Start React — terminal B

```bash
set -a
source .env.local
set +a
npm --prefix frontend run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. The browser talks only to FastAPI; it never
receives the wallet private key or Pinata token.

### 4. Start the blockchain listener — terminal C

```bash
source backend/.venv/bin/activate
set -a
source .env.local
set +a
python listener/claims_listener.py
```

A healthy listener prints the contract address, Kafka topic, and checkpoint.
The checkpoint lets it resume after a restart without beginning from block zero.

### 5. Start the scoring worker — terminal D

```bash
source backend/.venv/bin/activate
set -a
source .env.local
set +a
python -m integrations.kafka.scoring_worker
```

The first import may build a Matplotlib font cache; that is normal. A healthy
worker prints the topic and consumer-group name, then waits for a new claim.

### 6. Submit and follow one claim

Keep the listener and worker running, then submit the pre-filled fictional claim
from the browser.

You should see this sequence:

1. The browser shows the Sepolia submission transaction, IPFS pointer, and hash.
2. The listener prints `IPFSVerified` followed by `KafkaPublished`.
3. The worker prints `ClaimAssessed`.
4. The browser replaces the pending message with the XGBoost probability, SHAP
   indicators, on-chain score, assessment transaction, and lifecycle status.
5. The newest row appears in **All submitted claims**.

Click **View details →** on any row to reopen that claim. The page keeps the
latest public receipt after a refresh and restores the newest contract claim if
browser storage is empty.

## Run the automated checks

After the first-time installation, run the checks from the repository root:

```bash
# Python: backend, model, listener and integrations
source backend/.venv/bin/activate
python -m pytest \
  listener/test_*.py integrations/ipfs/tests integrations/kafka/tests \
  integrations/postgres/tests backend/tests model/tests -q

# Smart contract
cd contract
npm ci
npx hardhat test

# Frontend
cd ../frontend
npm ci
npm test
npm run lint
npm run build
```

The live Kafka producer/consumer smoke test is deliberately opt-in:

```bash
KAFKA_INTEGRATION_TEST=true \
  backend/.venv/bin/python -m pytest \
  integrations/kafka/tests/test_integration.py -q
```

## Contract lifecycle

The registry uses five statuses:

| Value | Status | Meaning |
| ---: | --- | --- |
| `0` | `Submitted` | Recorded and awaiting assessment |
| `1` | `UnderReview` | Scored but still requires human review |
| `2` | `Approved` | Final accepted outcome |
| `3` | `Rejected` | Final rejected outcome |
| `4` | `Flagged` | Model score exceeded the demonstration threshold |

The model never approves or rejects a claim automatically. A low score becomes
`UnderReview`, while a score above the saved threshold becomes `Flagged`.

In the normal asynchronous flow, `Submitted` can be brief because the worker may
score the claim before the dashboard refreshes. `Approved` and `Rejected` are
reserved for a future authenticated human-review workflow.

## Understanding the model result

XGBoost returns a probability between `0` and `1`. Solidity has no floating-point
type, so the worker multiplies the probability by `10,000` and stores a whole
number:

```text
probability 0.2466 = 24.66% = on-chain score 2,466 / 10,000
threshold   0.4700 = 47.00% = threshold score 4,700 / 10,000
```

The three displayed SHAP indicators are the features that moved that individual
prediction most. They explain the model's behaviour; they do not prove fraud or
show that a feature caused fraud.

Older claims can show a score without current XGBoost/SHAP details if they were
created before the PostgreSQL assessment history. The interface says so rather
than inventing missing model information.

## Common local problems

### The worker shows `Coordinator load in progress`

Kafka is still starting its internal coordinator. Wait a few seconds and leave
the process running. If it continues, check:

```bash
docker compose -f integrations/kafka/compose.yml ps
```

### The worker fails on `schemaVersion: 1`

The local Kafka volume contains an older message from before claim schema v2.
Stop the worker and, only if those old local test messages are no longer needed,
move this development consumer group to the latest offsets:

```bash
docker compose -f integrations/kafka/compose.yml exec kafka \
  /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group claims-registry-scorer-v1 \
  --topic claims.submitted.v1 \
  --reset-offsets \
  --to-latest \
  --execute
```

This skips old messages; it does not delete the topic. A production worker should
quarantine incompatible messages in a dead-letter workflow instead.

### A claim remains `Submitted`

Check that both `listener/claims_listener.py` and
`integrations.kafka/scoring_worker.py` are running. Then confirm Kafka consumer
lag in <http://127.0.0.1:8081>.

### Every new claim is `UnderReview`

That is expected when its probability is below the current 47% threshold.
`Flagged` appears only at or above the threshold. Neither status is a final claim
decision.

### XGBoost cannot load `libomp`

On macOS:

```bash
brew install libomp
```

Then restart the Python process.

## Stop the local application

Stop each foreground process with `Ctrl+C`, then stop Docker services:

```bash
docker compose -f integrations/kafka/compose.yml down
```

Do not add `--volumes` unless you deliberately want to delete local Kafka
messages and PostgreSQL assessments.

## Security and production limitations

This repository demonstrates integration, not a production insurance platform.
The evidence field is intentionally disabled because a public CID is an address,
not a password. Anyone who learns the CID can request the unencrypted bytes while
an IPFS node continues to provide them.

Before accepting real claims or evidence, the design would need at least:

- per-file envelope encryption before IPFS and managed off-chain keys;
- audited role-based contract access control;
- managed transaction signing instead of a process-level private key;
- authenticated users, authorization and formal audit-retention controls;
- file validation, malware scanning, access logs, and deletion procedures;
- an indexed event history rather than repeated direct contract reads;
- a validated real insurance-fraud dataset and monitored model;
- managed Kafka with TLS/SASL, replication, dead-letter handling, and monitoring.

For a production deployment, inject secrets through a managed secret store and
use a cloud KMS or Vault-style service for encryption keys. Never place a
decryption key, wallet private key, or Pinata token on IPFS, Sepolia, or in a
`VITE_` browser variable.
