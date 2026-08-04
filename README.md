# Decentralized Claims Registry

A research application that anchors a synthetic insurance claim on Ethereum,
screens it off-chain, and leaves a compact result that anyone can verify.

> **Use fictional data only.** Claim JSON is uploaded to public, unencrypted
> IPFS. Sepolia transactions and pointers are public. The model is trained on a
> synthetic dataset and must never be used to approve, reject, or investigate a
> real claim.

## One claim, end to end

```mermaid
flowchart LR
    Browser["React browser"] -->|"claim + insurer API key"| API["FastAPI"]
    API -->|"signed canonical JSON"| IPFS[("Public IPFS")]
    API -->|"Keccak hash + CID"| Chain[("Sepolia registry")]
    Chain -->|"confirmed ClaimSubmitted"| Listener["Listener"]
    Listener -->|"verified reference event"| Kafka[("Kafka")]
    Kafka --> Worker["Scoring worker"]
    IPFS -->|"same bytes"| Worker
    Worker -->|"features, duplicate check, score, SHAP"| Postgres[("PostgreSQL")]
    Worker -->|"status + score"| Chain
    Postgres -->|"assessment details"| API
    Chain -->|"current claim state"| API
    API --> Browser
```

The split is intentional: the request finishes after the IPFS document has been
verified and its hash has been anchored. Kafka handles the slower screening
work without holding the browser request open.

## What is stored where

| Place | Stored | Deliberately not stored |
| --- | --- | --- |
| Browser | Form state in memory; latest public receipt in local storage | Wallet keys, Pinata JWT, HMAC keys, saved insurer credential |
| IPFS | Signed schema-v4 synthetic claim JSON | Encryption or access control |
| Sepolia | Claim ID, submitter, hash, CID, status, score, timestamps | Full claim, SHAP reasons, private fingerprints |
| Kafka | Versioned blockchain and IPFS references | Full claim payload |
| PostgreSQL | Versioned features, keyed fingerprints, score, SHAP reasons, write receipt | HMAC keys, raw policy reference, description, evidence |

## Trust and replay boundaries

```mermaid
sequenceDiagram
    participant A as FastAPI
    participant I as IPFS
    participant E as Sepolia
    participant L as Listener
    participant K as Kafka
    participant W as Worker
    participant P as PostgreSQL

    A->>I: Upload exact canonical bytes
    A->>I: Download and compare bytes
    A->>E: Submit hash and CID
    E-->>L: Confirmed ClaimSubmitted log
    L->>I: Download and verify on-chain hash
    L->>K: Publish deterministic event ID
    W->>I: Reverify hash and gateway authorization
    W->>P: Save immutable feature snapshot and score
    W->>E: Write UnderReview or Flagged
    W->>P: Mark chain write complete
    W->>K: Commit offset
```

- The listener advances its block checkpoint only after the whole range has
  been handled and Kafka has acknowledged publication.
- The worker commits a Kafka offset only after persistence and chain write-back
  succeed.
- The blockchain log creates a deterministic `event_id`, so a restart can
  replay safely instead of silently skipping a claim.
- Completed scores are reused on replay; the worker does not silently rescore a
  claim with a newer model.

## Repository map

```text
apps/
├── backend/       FastAPI, authentication, IPFS and Sepolia submission
├── contracts/     Solidity registry, tests and Ignition deployments
├── frontend/      React intake, receipt and claims dashboard
└── listener/      Confirmed-log polling, IPFS verification and checkpoints

packages/
├── duplicates/    Cross-insurer HMAC incident matching
├── integrations/  Ethereum, IPFS, Kafka and PostgreSQL adapters
├── model/         Training pipeline and serving-time XGBoost scorer
└── observability/ Structured logs, Prometheus metrics and shutdown handling

infrastructure/gcp/  Disposable single-VM research deployment
```

## Local setup

### Prerequisites

- Node.js 24 and npm
- Python 3.13
- Docker Desktop
- a Sepolia RPC endpoint
- separate Sepolia-only submitter and assessor wallets with test ETH
- a Pinata JWT for public test uploads

Never use a wallet that holds real assets.

### 1. Install dependencies

Run from the repository root:

```bash
python3 -m venv apps/backend/.venv
source apps/backend/.venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock

npm --prefix apps/frontend ci
npm --prefix apps/contracts ci
```

On macOS, XGBoost also needs OpenMP:

```bash
brew install libomp
```

### 2. Create the local configuration

```bash
cp .env.example .env.local
```

Set at least:

- `SEPOLIA_SUBMITTER_PRIVATE_KEY`
- `SEPOLIA_ASSESSOR_PRIVATE_KEY`
- `PINATA_JWT`

Keep `CLAIMS_DEPLOYMENT_ID="sepolia-security-audit-v1"` unless you have reviewed
and checked in another hardened Ignition deployment. The example file already
contains fictional local insurer credentials and safe local service addresses.
See the [backend guide](apps/backend/README.md#local-insurer-credentials) before
changing authentication settings.

Load the file in every terminal that runs an application process:

```bash
set -a
source .env.local
set +a
```

### 3. Build the reviewed model artifact

```bash
source apps/backend/.venv/bin/activate
python -m packages.model.train_xgboost --download
```

This downloads the pinned synthetic dataset, verifies its checksum, and writes
the ignored serving artifact under
`packages/model/artifacts/xgboost-african-motor-v1/`.

### 4. Start Kafka, PostgreSQL, and migrations

```bash
docker compose -f packages/integrations/kafka/compose.yml up -d
docker compose -f packages/integrations/kafka/compose.yml ps

python -m packages.integrations.postgres.migrations upgrade
python -m packages.integrations.postgres.migrations check
```

Kafka UI is available at <http://127.0.0.1:8081>.

### 5. Start the four application processes

Use a separate configured terminal for each command:

```bash
# FastAPI
uvicorn apps.backend.app.main:app --reload --host 127.0.0.1 --port 8000

# React
npm --prefix apps/frontend run dev -- --host 127.0.0.1

# Confirmed-block listener
python -m apps.listener.claims_listener

# Kafka scoring worker
python -m packages.integrations.kafka.scoring_worker
```

Open <http://127.0.0.1:5173>. Health and API documentation are available at:

- <http://127.0.0.1:8000/health/live>
- <http://127.0.0.1:8000/health/ready>
- <http://127.0.0.1:8000/docs>

### 6. Follow a claim

Submit the pre-filled fictional claim. A healthy run looks like this:

```text
Browser receipt: claim anchored, assessment pending
Listener log:    ipfs.verified -> kafka.claim_published
Worker log:      claim.assessed
Browser receipt: duplicate check + score + SHAP reasons + chain status
```

To demonstrate duplicate screening, submit the same incident fields through a
second fictional insurer while changing its claim and policy references. The
second claim should point to the first as a possible cross-insurer match.

## Claim lifecycle

```mermaid
stateDiagram-v2
    [*] --> Submitted
    Submitted --> UnderReview: score below threshold
    Submitted --> Flagged: score at or above threshold
    UnderReview --> Approved: future human review
    UnderReview --> Rejected: future human review
    UnderReview --> Flagged: later review
    Flagged --> Approved: future human review
    Flagged --> Rejected: future human review
```

The worker can create only `UnderReview` or `Flagged`. It never infers
`Approved` or `Rejected`. The on-chain score is the probability multiplied by
10,000: `0.2466` becomes `2,466`, displayed as `24.66%`.

## Checked-in Sepolia deployment

| Item | Value |
| --- | --- |
| Chain | Sepolia (`11155111`) |
| Deployment ID | `sepolia-security-audit-v1` |
| Module | `ClaimsRegistryModule#ClaimsRegistry` |
| Contract | `0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB` |
| Explorer | [Open in Sepolia Etherscan](https://sepolia.etherscan.io/address/0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB) |

Every runtime process resolves the address and ABI from the same deployment ID
and rejects a wrong chain, missing bytecode, legacy interface, or unauthorized
write wallet.

## Verification

```bash
# Python lint, unit tests, and coverage
source apps/backend/.venv/bin/activate
ruff check apps packages --exclude packages/model/notebooks
python -m pytest -m "not integration"

# Frontend
npm --prefix apps/frontend test
npm --prefix apps/frontend run lint
npm --prefix apps/frontend run build
npm --prefix apps/frontend run test:e2e

# Contract
npm --prefix apps/contracts exec -- hardhat test
npm --prefix apps/contracts exec -- hardhat build --build-profile production
```

Infrastructure-backed tests require the local Kafka and PostgreSQL services:

```bash
TEST_DATABASE_URL=postgresql://claims:claims-local@127.0.0.1:5432/claims_registry \
TEST_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092 \
  python -m pytest -m integration
```

## Limits that matter

- IPFS content is public and unencrypted.
- Insurer rate limits are process-local and reset on API restart.
- The dashboard reads current contract state directly; it is not an event index.
- Exact incident fingerprints produce review candidates, not proof of fraud.
- The synthetic model is an integration artifact, not a validated decision model.
- Wallets are process-level testnet signers, not managed signing infrastructure.
- Local and GCP Compose files use one Kafka broker and one PostgreSQL instance.

Before real claim data, the design would need encryption before IPFS, managed
keys and signing, enterprise identity, malware and evidence controls, an indexed
read model, managed replicated infrastructure, retention rules, and a validated
and monitored model.

## Focused guides

| Area | Guide |
| --- | --- |
| FastAPI and insurer credentials | [Backend](apps/backend/README.md) |
| Claim limits and controlled test bypass | [Rate-limit runbook](docs/rate-limiting-and-authorised-test-bypass.md) |
| Browser behaviour | [Frontend](apps/frontend/README.md) |
| Block polling and recovery | [Listener](apps/listener/README.md) |
| Roles and lifecycle rules | [Smart contract](apps/contracts/README.md) |
| Training and SHAP | [Model](packages/model/README.md) |
| Notebook workflow | [Notebooks](packages/model/notebooks/README.md) |
| Public file storage | [IPFS](packages/integrations/ipfs/README.md) |
| Event delivery and replay | [Kafka](packages/integrations/kafka/README.md) |
| Feature and assessment storage | [PostgreSQL](packages/integrations/postgres/README.md) |
| Single-VM deployment | [Google Cloud](infrastructure/gcp/README.md) |

Stop local infrastructure without deleting its volumes:

```bash
docker compose -f packages/integrations/kafka/compose.yml down
```
