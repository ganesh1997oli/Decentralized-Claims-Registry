# Decentralized Claims Registry

A dissertation prototype that records verifiable insurance-claim references on
Ethereum while keeping the claim document off-chain. The application combines a
Solidity registry, public IPFS storage, a FastAPI backend, a React interface, a
transparent demonstration scorer, an XGBoost/SHAP scoring worker, Kafka, and
PostgreSQL assessment storage.

> **Research prototype:** the current workflow uses synthetic claim data,
> unencrypted public IPFS, a Sepolia test wallet, and a demonstration model. Do
> not enter real names, addresses, policy details, photographs, or documents.

## What the application does

When a user submits a synthetic claim:

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

Set `CLAIM_SCORING_MODE="inline_demo"` to preserve the earlier synchronous
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

Never use a wallet that holds real assets. Local development uses one ignored
file at the repository root:

```bash
cp .env.example .env.local
```

Add your Sepolia private key and Pinata JWT to `.env.local`. The tracked example
contains all other safe local defaults. Load the same file once in every new
terminal before starting an application process:

```bash
set -a
source .env.local
set +a
```

Python and Vite then read the values from the process environment. Contract
deployment continues to use Hardhat's encrypted keystore as described in the
contract guide. Only variables beginning with `VITE_` are exposed to browser
code, so never put a private key or token in a `VITE_` variable.

For a deployed environment, do not copy `.env.local` to a server or container.
Inject the required settings into each process through the hosting platform's
secret manager.

## Quick start on Sepolia

The contract is already deployed. The asynchronous application run uses the
processes below; keep long-running commands in separate terminals.

### 1. Start the backend

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt -r model/requirements.txt \
  -r integrations/kafka/requirements.txt

set -a
source .env.local
set +a

uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Check the service at <http://127.0.0.1:8000/health> or open the interactive API
documentation at <http://127.0.0.1:8000/docs>.

### 2. Start the frontend

```bash
set -a
source .env.local
set +a
npm --prefix frontend install
npm --prefix frontend run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173> and keep the page ready. Complete the listener and
worker steps below before submitting when `async_xgboost` is enabled.

### 3. Train the reviewed XGBoost artifact

```bash
source backend/.venv/bin/activate
python -m model.train_xgboost --download
```

### 4. Start Kafka and PostgreSQL

Start Docker Desktop, then run:

```bash
docker compose -f integrations/kafka/compose.yml up -d
```

The root example already enables Kafka and matches the local Compose ports.

### 5. Publish verified blockchain events

```bash
source backend/.venv/bin/activate
pip install -r listener/requirements.txt

set -a
source .env.local
set +a

python listener/claims_listener.py
```

### 6. Run the XGBoost scoring worker

```bash
set -a
source .env.local
set +a
python -m integrations.kafka.scoring_worker
```

Keep the listener and worker running before submitting a new claim. The listener
prints `KafkaPublished`; the worker prints `ClaimAssessed`; and the browser
updates when PostgreSQL contains the result.

## Run the automated checks

Install the backend and listener requirements first, then run the project checks:

```bash
# Python: backend, model, listener and integrations
source backend/.venv/bin/activate
pip install -r listener/requirements.txt
pip install -r model/requirements.txt
python -m pytest \
  listener/test_*.py integrations/ipfs/tests integrations/kafka/tests \
  integrations/postgres/tests backend/tests model/tests -q

# Smart contract
cd contract
npm install
npx hardhat test

# Frontend
cd ../frontend
npm install
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

## Security and production limitations

This repository demonstrates integration, not a production insurance platform.
Before processing real claims, the design would need at least:

- encrypted private storage or client-side envelope encryption before IPFS;
- audited role-based contract access control;
- managed transaction signing instead of a process-level private key;
- authenticated users, authorization and formal audit-retention controls;
- an indexed event history rather than repeated direct contract reads;
- a validated real insurance-fraud dataset and monitored model;
- managed Kafka with TLS/SASL, replication and operational monitoring.

Public IPFS content cannot be made private by hiding its CID. Anyone who obtains
the CID can request the unencrypted bytes from an available gateway.
