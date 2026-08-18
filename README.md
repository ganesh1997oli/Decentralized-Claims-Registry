# Decentralized Claims Registry

A research application that anchors a synthetic insurance claim on Ethereum,
screens it off-chain, and leaves a compact result that anyone can verify.

> **Use fictional data only.** Claim documents are encrypted before they are
> uploaded to public IPFS, but CIDs, Sepolia transactions, lifecycle states and
> timing remain public. This repository has not completed the organisational,
> legal, key-custody or independent security assurance needed for real claims.

## One claim, end to end

```mermaid
flowchart LR
    Browser["React browser"] -->|"wallet proof + claim + policy"| API["FastAPI"]
    API -->|"AES-256-GCM claim envelope"| IPFS[("Public IPFS")]
    API -->|"scoped one-time insurer permit"| Permit["Permit signer"]
    API -->|"exact EIP-712 request"| Browser
    Browser -->|"claimant submitter signature"| API
    API --> Outbox[("PostgreSQL relay outbox")]
    Relayer["Restricted gas relayer"] --> Outbox
    Relayer -->|"ERC-2771 execute"| Chain[("Sepolia registry")]
    Chain -->|"confirmed ClaimSubmitted"| Listener["Listener"]
    Listener -->|"idempotent claim projection"| Postgres[("PostgreSQL")]
    Listener -->|"verified reference event"| Kafka[("Kafka")]
    Kafka --> Worker["Scoring worker"]
    IPFS -->|"hash verify, then decrypt"| Worker
    Worker -->|"features, duplicate check, score, SHAP"| Postgres
    Worker -->|"status + score"| Chain
    Assessor["Human assessor console"] -->|"private fraud outcome revision"| Postgres
    Governance["Insurer governance console"] -->|"audited decision proposal"| Postgres
    Governance -->|"separate decision wallet"| Chain
    Postgres -->|"assessment details"| API
    Postgres -->|"authenticated human outcome"| API
    Postgres -->|"confirmed indexed claims"| API
    API --> Browser
```

The split is intentional: policy eligibility and the insurer permit authorize
the claim, the claimant or representative authorizes the exact call, and an
isolated relayer only pays gas. Kafka handles slower screening without holding
the submission request open. See the
[public-intake design and provisioning guide](docs/README.md).

## What is stored where

| Place      | Stored                                                                                                                                                 | Deliberately not stored                                      |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------ |
| Browser    | Form state and short claimant session in memory; latest public receipt in local storage                                                                 | Wallet keys, Pinata JWT, encryption master keys, HMAC keys, saved bearer sessions |
| IPFS       | Versioned AES-256-GCM envelope containing the authorized schema-v6 claim; wrapped per-claim data key                                                 | Plaintext claim data or key-encryption keys                  |
| Sepolia    | Claim ID, claimant, insurer, submitter, claimant commitment, encrypted-envelope hash, CID, status, score, decision hash, timestamps                   | Full claim, decision rationale, SHAP reasons, private fingerprints |
| Kafka      | Versioned blockchain and IPFS references                                                                                                               | Full claim payload                                           |
| PostgreSQL | Gasless idempotency/outbox/transaction attempts; confirmed public index and event history; features, keyed fingerprints, score and SHAP reasons; private human-outcome revisions; append-only coverage-decision proposals | Encryption/HMAC keys, raw policy reference, description, evidence |

## Trust and replay boundaries

```mermaid
sequenceDiagram
    participant A as FastAPI
    participant I as IPFS
    participant R as Relayer
    participant E as Forwarder + registry
    participant L as Listener
    participant K as Kafka
    participant W as Worker
    participant P as PostgreSQL

    A->>A: Encrypt canonical claim with a fresh data key
    A->>I: Upload exact envelope bytes
    A->>I: Download and compare envelope bytes
    A->>P: Persist wallet-authorized request
    R->>P: Persist signed raw transaction before broadcast
    R->>E: Execute exact request and pay gas
    E-->>L: Confirmed ClaimSubmitted log
    L->>P: Append event and update claim projection
    L->>I: Download and verify on-chain hash
    L->>K: Publish deterministic event ID
    W->>I: Reverify envelope hash
    W->>W: Unwrap data key and authenticate/decrypt claim
    W->>P: Save immutable feature snapshot and score
    W->>E: Write UnderReview or Flagged
    W->>P: Mark chain write complete
    W->>K: Commit offset
```

- The listener advances its PostgreSQL block checkpoint only after the whole
  range has been indexed and Kafka has acknowledged publication.
- The worker commits a Kafka offset only after persistence and chain write-back
  succeed.
- The blockchain log creates a deterministic `event_id`, so a restart can
  replay safely instead of silently skipping a claim.
- Completed scores are reused on replay; the worker does not silently rescore a
  claim with a newer model.
- Human fraud conclusions remain private PostgreSQL evidence. A separately
  authenticated insurer operator prepares a coverage decision, and a distinct
  scoped wallet must explicitly sign the final `Approved` or `Rejected`
  transaction.

## Repository map

```text
apps/
├── backend/       Keyless FastAPI, authentication, IPFS and EIP-712 preparation
├── contracts/     Solidity registry, tests and Ignition deployments
├── frontend/      React intake, receipt and claims dashboard
├── listener/      Confirmed-log indexing, IPFS verification and reconciliation
└── relayer/       Durable nonce, fee replacement, broadcast and confirmation

packages/
├── duplicates/    Cross-insurer HMAC incident matching
├── integrations/  Ethereum, IPFS, Kafka and PostgreSQL adapters
├── model/         Training pipeline and serving-time XGBoost scorer
└── observability/ Structured logs, Prometheus metrics and shutdown handling

infrastructure/gcp/  Disposable single-VM research deployment
```

## Run the complete application locally

The application has five long-running processes in addition to PostgreSQL and
Kafka. Start them in order; otherwise the first visible error is often only a
downstream symptom of a missing migration, topic, or model artifact.

The detailed [local development guide](LOCAL_DEVELOPMENT.md) explains every
setting, wallet role, terminal, readiness check, failure state, and shutdown
step. The concise sequence is below.

### 1. Install prerequisites and dependencies

You need Python 3.13, Node.js 24, npm, Docker Desktop, a browser wallet, a
Sepolia RPC URL, Pinata JWT, and fictional Sepolia role wallets. Never use a
wallet that holds real assets.

```bash
cd <folder_directory>Decentralized-Claims-Registry

python3 -m venv apps/backend/.venv
apps/backend/.venv/bin/python -m pip install --require-hashes \
  -r requirements-dev.lock

npm --prefix apps/frontend ci
npm --prefix apps/contracts ci
```

On macOS, install XGBoost's OpenMP runtime once with `brew install libomp`.
Using the explicit `apps/backend/.venv/bin/python` path avoids accidentally
running the root virtual environment or a system Python without dependencies.

### 2. Deploy and configure public intake and governance

```bash
test -f .env.local || cp .env.example .env.local
```

Review `.env.local`; do not merely source it unchanged. The previous
`sepolia-public-intake-v1` predates the governed coverage-decision interface.
Deploy this branch, record its block, save its Ignition artifacts under a new
deployment ID, provision all five distinct contract roles, and configure claim
encryption before accepting writes. The application can read the old artifact,
but `/governance` fails closed until the selected artifact supports
`decideClaim`, `isDecisionMakerFor`, and `ClaimDecided`.

Build the reviewed synthetic model artifact:

```bash
apps/backend/.venv/bin/python \
  -m packages.model.train_xgboost --download
```

### 3. Start infrastructure, migrate, and create the topic

```bash
docker compose -f packages/integrations/kafka/compose.yml up -d
docker compose -f packages/integrations/kafka/compose.yml ps

set -a
source .env.local
set +a

apps/backend/.venv/bin/python \
  -m packages.integrations.postgres.migrations upgrade
apps/backend/.venv/bin/python \
  -m packages.integrations.postgres.migrations check

docker compose -f packages/integrations/kafka/compose.yml exec kafka \
  /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 \
  --create --if-not-exists \
  --topic "$KAFKA_CLAIM_SUBMITTED_TOPIC" \
  --partitions 3 --replication-factor 1
```

Kafka UI is available at <http://127.0.0.1:8081>.

### 4. Start each application process

Open five terminal tabs. In every tab, change to the repository root and load
the local configuration:

```bash
cd <folder_directory>Decentralized-Claims-Registry
set -a; source .env.local; set +a
```

Then run exactly one command per terminal:

```bash
# Terminal A — transaction-keyless FastAPI
apps/backend/.venv/bin/python -m uvicorn \
  apps.backend.app.main:app --reload --host 127.0.0.1 --port 8000

# Terminal B — React/Vite
npm --prefix apps/frontend run dev -- --host 127.0.0.1

# Terminal C — sponsored-transaction relayer
apps/backend/.venv/bin/python -m apps.relayer.gasless_relayer

# Terminal D — confirmed-block listener and PostgreSQL indexer
apps/backend/.venv/bin/python -m apps.listener.claims_listener

# Terminal E — Kafka scoring and assessment worker
apps/backend/.venv/bin/python \
  -m packages.integrations.kafka.scoring_worker
```

For better local key separation, unset unrelated wallet variables in each
terminal as shown in the [full terminal instructions](LOCAL_DEVELOPMENT.md#6-start-the-five-application-terminals).

### 5. Verify before submitting

```bash
curl --fail --silent http://127.0.0.1:8000/health/ready \
  | apps/backend/.venv/bin/python -m json.tool
curl --fail --silent http://127.0.0.1:8000/claims/gasless/config \
  | apps/backend/.venv/bin/python -m json.tool
```

Readiness should report every dependency as `ok`. It intentionally reports
`coverage_governance` unavailable while an older deployment artifact is selected.
Open:

- claims application: <http://127.0.0.1:5173>
- API documentation: <http://127.0.0.1:8000/docs>
- indexer operations: <http://127.0.0.1:5173/operations>
- human fraud review: <http://127.0.0.1:5173/assessor>
- insurer coverage governance: <http://127.0.0.1:5173/governance>
- Kafka UI: <http://127.0.0.1:8081>

In the claims page, connect a claimant or authorized representative wallet and
enter the configured synthetic policy reference. The wallet first proves
ownership, then signs the exact EIP-712 request without paying gas. A healthy
run progresses through:

```text
Browser:  prepared -> wallet signed -> authorized
Relayer:  signed -> broadcast -> confirmed
Listener: IPFS verified -> Kafka event published -> checkpoint advanced
Worker:   features + duplicate check + score -> assessment confirmed
Browser:  public anchor + review signals + indexed current state
```

The operations page similarly accepts the raw operations key, while FastAPI
stores only `INDEXER_OPERATIONS_API_KEY_SHA256`. Restart FastAPI after changing
that digest.

## Claim lifecycle

```mermaid
stateDiagram-v2
    [*] --> Submitted
    Submitted --> UnderReview: scoped scoring assessor
    Submitted --> Flagged: scoped scoring assessor
    UnderReview --> Approved: scoped decision maker
    UnderReview --> Rejected: scoped decision maker
    Flagged --> Approved: scoped decision maker
    Flagged --> Rejected: scoped decision maker
```

The worker can create only `UnderReview` or `Flagged`. It never infers
`Approved` or `Rejected`. The on-chain score is the probability multiplied by
10,000: `0.2466` becomes `2,466`, displayed as `24.66%`.

The separate `/assessor` console records one of `ConfirmedFraud`, `Legitimate`,
or `Inconclusive` in PostgreSQL after model screening. Corrections append a new
revision instead of overwriting history. These human outcomes do not change the
contract status: `Approved`/`Rejected` remain business coverage decisions and
are never converted automatically into fraud labels. The `/governance` console
requires a digest-only insurer-scoped API credential to prepare an append-only
proposal and a different `DECISION_MAKER_ROLE` wallet to broadcast it. The
listener confirms the proposal only after the `ClaimDecided` log reaches the
configured confirmation depth. `Inconclusive` cannot support a binary decision,
and the application performs no automatic model retraining.

## Sepolia deployments

| Purpose                       | Deployment ID               | Registry                                                                                            | Forwarder                                                                                         | Start block |
| ----------------------------- | --------------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ----------- |
| Permit-backed public intake   | `sepolia-public-intake-v1`  | [`0xb64B...7dff`](https://sepolia.etherscan.io/address/0xb64BaB321e0Fb19b2295f8182D5A37bAf85F7dff) | [`0xeff6...0BD0`](https://sepolia.etherscan.io/address/0xeff61937C6a11236D87863e763c13cd7083f0BD0) | `11516697` |
| Previous gasless flow (no public permits) | `sepolia-gasless-v1` | [`0x5A7A...A300`](https://sepolia.etherscan.io/address/0x5A7A3e22843397f998823D0d58aBd2E1f4b2A300) | [`0x0e68...5F0`](https://sepolia.etherscan.io/address/0x0e68Ac27a344f454373604Eec3144c427661E5F0) | `11426492` |
| Read-only legacy history      | `sepolia-security-audit-v1` | [`0x2AbA...B78cB`](https://sepolia.etherscan.io/address/0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB) | None                                                                                              | `11377814`  |

All use Sepolia chain ID `11155111`. Runtime selection is explicit through
`CLAIMS_DEPLOYMENT_ID`; the listener checkpoint and projection are additionally
scoped by chain and registry address. Public writes fail closed unless the
selected artifact contains the current permit-backed interface.

## Verification

```bash
# Python lint, unit tests, and coverage
apps/backend/.venv/bin/python -m ruff check \
  apps packages --exclude packages/model/notebooks
apps/backend/.venv/bin/python -m pytest -m "not integration"

# Frontend
npm --prefix apps/frontend test
npm --prefix apps/frontend run lint
npm --prefix apps/frontend run build
npm --prefix apps/frontend run test:e2e

# Contract (Hardhat resolves its config from this directory)
cd apps/contracts
npm exec -- hardhat test
npm exec -- hardhat build --build-profile production
cd ../..
```

Infrastructure-backed tests require the local Kafka and PostgreSQL services:

```bash
TEST_DATABASE_URL=postgresql://claims:claims-local@127.0.0.1:5432/claims_registry \
TEST_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092 \
  apps/backend/.venv/bin/python -m pytest -m integration
```

## Limits that matter

- Encrypted IPFS envelopes are public and permanent; confidentiality depends on
  protecting and rotating the external wrapping key. Production mode rejects
  local keys and requires Google Cloud KMS.
- Valid sponsorship quotas are durable in PostgreSQL; early/invalid-credential
  abuse counters remain process-local and should be enforced at the edge in a
  multi-instance production deployment.
- The dashboard reads a confirmed-event PostgreSQL index and may lag the chain
  by the configured confirmation depth.
- Exact incident fingerprints produce review candidates, not proof of fraud.
- The synthetic model is an integration artifact, not a validated decision model.
- Insurers sign through browser test wallets; relayer, assessor and decision
  wallets are testnet identities, not managed signing infrastructure.
- Local and GCP Compose files use one Kafka broker and one PostgreSQL instance.

Before real claim data, the implementation would still need enterprise identity
and access reviews, HSM/managed signing for every chain role, independent
security and smart-contract audits, malware and evidence controls, managed
replicated infrastructure, explicit deep-reorganization recovery, retention
and key-destruction policy, and a validated and monitored model.

## Focused guides

| Area                                              | Guide                                                                                         |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Complete local startup                            | [Local development](LOCAL_DEVELOPMENT.md)                                                     |
| FastAPI, claimant sessions, policies and permits  | [Backend](apps/backend/README.md)                                                              |
| Gasless relayer                                   | [Relayer](apps/relayer/README.md)                                                              |
| Gasless security and operations                   | [Production gasless runbook](apps/relayer/README.md#production-gasless-claim-transactions)    |
| Indexer monitoring and recovery                   | [Indexer operations runbook](apps/listener/README.md#indexer-operations-runbook)              |
| Claim limits and controlled test bypass           | [Rate-limit runbook](apps/backend/README.md#public-claim-intake-limits)                        |
| Cross-insurer duplicate screening                 | [Duplicate screening](packages/duplicates/README.md)                                          |
| Browser behaviour                                 | [Frontend](apps/frontend/README.md)                                                            |
| Block polling and recovery                        | [Listener](apps/listener/README.md)                                                            |
| Roles and lifecycle rules                         | [Smart contract](apps/contracts/README.md)                                                     |
| Training and SHAP                                 | [Model](packages/model/README.md)                                                              |
| Notebook workflow                                 | [Notebooks](packages/model/notebooks/README.md)                                                |
| Public file storage                               | [IPFS](packages/integrations/ipfs/README.md)                                                   |
| Claim-envelope encryption                        | [Privacy](packages/integrations/privacy/README.md)                                             |
| Event delivery and replay                         | [Kafka](packages/integrations/kafka/README.md)                                                 |
| Feature and assessment storage                    | [PostgreSQL](packages/integrations/postgres/README.md)                                         |
| Single-VM deployment                              | [Google Cloud](infrastructure/gcp/README.md)                                                   |

Stop local infrastructure without deleting its volumes:

```bash
docker compose -f packages/integrations/kafka/compose.yml down
```
