# Decentralized Claims Registry

A dissertation prototype that records verifiable insurance-claim references on
Ethereum while keeping the claim document off-chain. The application combines a
Solidity registry, public IPFS storage, a FastAPI backend, a React interface, a
transparent synthetic fraud model, and an optional Kafka event stream.

> **Research prototype:** the current workflow uses synthetic claim data,
> unencrypted public IPFS, a Sepolia test wallet, and a demonstration model. Do
> not enter real names, addresses, policy details, photographs, or documents.

## What the application does

When a user submits a synthetic claim:

1. FastAPI validates the form and creates deterministic JSON bytes.
2. The local demonstration model calculates a fraud probability and short
   contributing reasons.
3. The backend uploads the exact JSON bytes to IPFS through Pinata and downloads
   them again to verify the upload.
4. The backend stores the IPFS pointer and the document's Keccak-256 hash in the
   `ClaimsRegistry` contract on Ethereum Sepolia.
5. The model result is written back as `UnderReview` or `Flagged`.
6. The React interface displays the receipt and a paginated view of submitted
   claims.
7. The optional listener verifies IPFS data against the on-chain hash and can
   publish the verified event to Kafka.

```text
React
  │
  ▼
FastAPI ──► synthetic fraud model
  │
  ├──────► Pinata / public IPFS (claim JSON)
  │
  └──────► Ethereum Sepolia (hash, CID, status and fraud score)
                                  │
                                  ▼
                         blockchain listener
                                  │
                                  ▼ optional
                                Kafka
```

## Project structure

| Directory | Responsibility | Documentation |
| --- | --- | --- |
| `contract/` | Solidity contract, tests and Ignition deployments | [Contract guide](contract/README.md) |
| `backend/` | FastAPI validation, scoring, IPFS and Sepolia workflow | [Backend guide](backend/README.md) |
| `frontend/` | React claim form, receipt and claims dashboard | [Frontend guide](frontend/README.md) |
| `model/` | Deterministic synthetic model training and inference | [Model guide](model/README.md) |
| `listener/` | Blockchain event polling, verification and checkpoints | [Listener guide](listener/README.md) |
| `integrations/ipfs/` | Shared Pinata and IPFS adapter | [IPFS guide](integrations/ipfs/README.md) |
| `integrations/kafka/` | Kafka messages, producer, consumer and local broker | [Kafka guide](integrations/kafka/README.md) |

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
- Docker Desktop, only if you want to run Kafka

Never use a wallet that holds real assets. Keep the private key and Pinata JWT in
ignored `.env.local` files and never expose them to the browser.

## Quick start on Sepolia

The contract is already deployed, so the normal application run needs the
backend and frontend. Run each process in a separate terminal from the repository
root.

### 1. Start the backend

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt

cp backend/.env.example backend/.env.local
# Add SEPOLIA_RPC_URL, SEPOLIA_PRIVATE_KEY and PINATA_JWT.
set -a; source backend/.env.local; set +a

uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Check the service at <http://127.0.0.1:8000/health> or open the interactive API
documentation at <http://127.0.0.1:8000/docs>.

### 2. Start the frontend

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>, submit the pre-filled synthetic claim, and wait for
the Sepolia receipt. The same page lists submitted claims, their current status,
and fraud score.

### 3. Observe and verify events (optional)

```bash
source backend/.venv/bin/activate
pip install -r listener/requirements.txt

cp listener/.env.example listener/.env.local
# Add SEPOLIA_RPC_URL and optionally your Pinata gateway.
set -a; source listener/.env.local; set +a

python listener/claims_listener.py
```

For a successful submission, the listener prints `ClaimSubmitted`,
`IPFSVerified`, and `ClaimAssessed` messages.

### 4. Stream verified events through Kafka (optional)

```bash
docker compose -f integrations/kafka/compose.yml up -d

set -a; source listener/.env.local; set +a
export KAFKA_ENABLED=true

# Terminal A
python -m integrations.kafka.consumer

# Terminal B
python listener/claims_listener.py
```

Start both processes before submitting a new claim. The listener should print
`KafkaPublished`; the consumer should print `KafkaProcessed`.

## Run the automated checks

Install the backend and listener requirements first, then run the project checks:

```bash
# Python: backend, model, listener and integrations
source backend/.venv/bin/activate
pip install -r listener/requirements.txt
python -m pytest \
  listener/test_*.py integrations/ipfs/tests integrations/kafka/tests \
  backend/tests model/tests -q

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
- authenticated users, authorization and an audit database;
- an indexed event history rather than repeated direct contract reads;
- a validated real insurance-fraud dataset and monitored model;
- managed Kafka with TLS/SASL, replication and operational monitoring.

Public IPFS content cannot be made private by hiding its CID. Anyone who obtains
the CID can request the unencrypted bytes from an available gateway.
