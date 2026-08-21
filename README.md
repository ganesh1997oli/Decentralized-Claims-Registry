# Decentralized Claims Registry

A research application that anchors a synthetic insurance claim on Ethereum,
screens it off-chain, and leaves a compact result that anyone can verify.

In plain English: the project proves that a claim existed in a specific form,
at a specific time, without putting the whole insurance workflow on-chain. The
public chain provides a tamper-evident anchor. The API, database, event pipeline,
and model do the work that is private, large, or too slow for a smart contract.

> **Use fictional data only.** Claim JSON is uploaded to public, unencrypted
> IPFS. Sepolia transactions and pointers are public. The model is trained on a
> synthetic dataset and must never be used to approve, reject, or investigate a
> real claim.

![Claims Registry public claim intake](docs/images/frontend-claim-intake.jpg)

_Public React claim intake. The page makes the storage boundary visible before
a user signs anything: compact evidence goes to Sepolia, synthetic JSON goes to
public IPFS, and secrets stay in server-side processes._

## Start here

This repository is a **research prototype**, not a production insurance
service. It is intentionally split into small applications so each security
boundary can be inspected and tested independently.

Choose the path that matches what you want to do:

| Your goal | Start with | External accounts needed |
| --- | --- | --- |
| Read and understand the design | [Project in one minute](#project-in-one-minute) | None |
| Check that the code works on your computer | [Quick setup check](#quick-setup-check-no-secrets-required) | None |
| Run the complete claim workflow | [Complete local workflow](#run-the-complete-application-locally) | Sepolia RPC, Pinata, test wallets, and provisioned contract roles |
| Work on one component | [Repository map](#repository-map) and [focused guides](#focused-guides) | Usually none for unit tests |

> **Important meaning of local:** the frontend, API, relayer, listener, worker,
> PostgreSQL, and Kafka run on your computer. A complete submission still writes
> to the public Ethereum Sepolia testnet and uploads the fictional claim JSON to
> public IPFS. This is not an offline-only demo.

### Project in one minute

For one fictional claim, the system does six main things:

1. The browser collects the claim and asks a wallet to prove who is submitting.
2. FastAPI checks the fictional policy and prepares the exact request to sign.
3. The claim JSON is uploaded to public IPFS and its fingerprint is anchored on
   Sepolia through a gas-paying relayer.
4. A listener waits for blockchain confirmations, verifies the IPFS bytes, and
   publishes a small reference event to Kafka.
5. A worker checks for an exact cross-insurer duplicate, runs the synthetic
   XGBoost model, saves SHAP reasons in PostgreSQL, and updates the public status.
6. The browser shows the public anchor and review signals. A human assessor can
   separately record an off-chain outcome.

No model result automatically approves, rejects, or accuses a person of fraud.

### Current implementation and earlier planning documents

The repository is the source of truth for what runs today. Earlier proposal
material describes the starting plan, and several decisions changed during
implementation and security review:

| Area | Current repository behavior |
| --- | --- |
| Blockchain network | Ethereum Sepolia, not Polygon Amoy |
| Submission | Claimant-signed EIP-712 request plus insurer-scoped permit and restricted gas relayer |
| IPFS privacy | Public and unencrypted, so only fictional claim JSON is allowed |
| On-chain data | Compact hash, CID, parties, status, score, and timestamps only |
| SHAP explanations | Stored off-chain in PostgreSQL and shown as review context |
| Model evaluation | Leakage-controlled temporal split with honest negative results against the simpler baseline |
| Human decision | Separate off-chain assessor outcome with no automatic approval, rejection, or retraining |

Do not copy setup values or architecture assumptions from an earlier proposal.
Use this README, `.env.example`, the checked-in deployment artifacts, and the
focused component guides together.

### Quick setup check, no secrets required

This path is the best first step for a new contributor. It installs the locked
dependencies and runs local checks without sending a transaction, uploading to
IPFS, starting Docker, or needing any private key.

#### Prerequisites

- Git
- Python 3.13
- Node.js 24 with npm
- About 3 GB of free disk space for environments, packages, and build output
- On macOS, Homebrew `libomp` for XGBoost

Confirm the tools are available:

```bash
# Each command should print a version instead of "command not found".
git --version
python3 --version
node --version
npm --version

# XGBoost needs the OpenMP runtime on macOS. Run this only on macOS.
brew install libomp
```

From the repository root, install the dependencies:

```bash
# Replace this example with the folder where you cloned the repository.
cd /path/to/Decentralized-Claims-Registry

# Create one isolated Python environment used by all Python components.
python3 -m venv apps/backend/.venv

# Install the exact reviewed Python dependency versions from the lockfile.
apps/backend/.venv/bin/python -m pip install --require-hashes \
  -r requirements-dev.lock

# Install the exact frontend and smart-contract dependency versions.
npm --prefix apps/frontend ci
npm --prefix apps/contracts ci
```

Run the fast checks:

```bash
# Python unit tests skip services that require PostgreSQL or Kafka.
apps/backend/.venv/bin/python -m pytest -m "not integration"

# Frontend tests use mocked API responses and do not contact Sepolia.
npm --prefix apps/frontend test

# A production build catches TypeScript and bundling errors.
npm --prefix apps/frontend run build

# Hardhat runs the Solidity and TypeScript contract tests locally.
cd apps/contracts
npm exec -- hardhat test
cd ../..
```

If these commands pass, the development toolchain is ready. You can study and
change most of the code without owning a testnet wallet. Continue below only if
you need the complete cross-service demonstration.

### Contents

- [End-to-end architecture](#one-claim-end-to-end)
- [Data locations and privacy boundary](#what-is-stored-where)
- [Repository map](#repository-map)
- [Component responsibilities](#component-responsibilities)
- [Glossary](#small-glossary)
- [Complete local workflow](#run-the-complete-application-locally)
- [Claim lifecycle](#claim-lifecycle)
- [Verification commands](#verification)
- [Important limitations](#limits-that-matter)
- [Component guides](#focused-guides)

## One claim end to end

```mermaid
flowchart LR
    Browser["React browser"] -->|"wallet proof + claim + policy"| API["FastAPI"]
    API -->|"verified schema-v6 JSON"| IPFS[("Public IPFS")]
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
    IPFS -->|"same bytes"| Worker
    Worker -->|"features, duplicate check, score, SHAP"| Postgres
    Worker -->|"status + score"| Chain
    Assessor["Human assessor console"] -->|"private fraud outcome revision"| Postgres
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
| Browser    | Form state, short claimant session in memory, and latest public receipt in local storage                                                                  | Wallet keys, Pinata JWT, HMAC keys, saved bearer sessions    |
| IPFS       | Authorized schema-v6 synthetic claim JSON                                                                                                              | Encryption or access control                                 |
| Sepolia    | Claim ID, claimant, insurer, submitter, claimant commitment, hash, CID, status, score, timestamps                                                      | Full claim, SHAP reasons, private fingerprints               |
| Kafka      | Versioned blockchain and IPFS references                                                                                                               | Full claim payload                                           |
| PostgreSQL | Gasless idempotency, outbox, and transaction attempts<br>Confirmed public index and event history<br>Versioned features, keyed fingerprints, score, and SHAP reasons<br>Private human-outcome revisions | HMAC keys, raw policy reference, description, evidence |

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

    A->>I: Upload exact canonical bytes
    A->>I: Download and compare bytes
    A->>P: Persist wallet-authorized request
    R->>P: Persist signed raw transaction before broadcast
    R->>E: Execute exact request and pay gas
    E-->>L: Confirmed ClaimSubmitted log
    L->>P: Append event and update claim projection
    L->>I: Download and verify on-chain hash
    L->>K: Publish deterministic event ID
    W->>I: Reverify hash and gateway authorization
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
- Completed scores are reused on replay. The worker does not silently rescore a
  claim with a newer model.

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

### Suggested code reading order

A beginner can follow one claim without reading every file:

1. [`ClaimForm.tsx`](apps/frontend/src/components/ClaimForm.tsx) collects and
   validates the fictional input.
2. [`gasless-submission.ts`](apps/frontend/src/gasless-submission.ts) coordinates
   wallet proof, preparation, EIP-712 signing, authorization, and polling.
3. [`main.py`](apps/backend/app/main.py) defines the HTTP boundary, while
   [`gasless_service.py`](apps/backend/app/gasless_service.py) owns the submission
   use case.
4. [`ClaimsRegistry.sol`](apps/contracts/contracts/ClaimsRegistry.sol) enforces
   permits, roles, lifecycle rules, and the compact public record.
5. [`gasless_relayer.py`](apps/relayer/gasless_relayer.py) moves a durable signed
   request through signing, broadcast, replacement, and confirmation.
6. [`claims_listener.py`](apps/listener/claims_listener.py) rebuilds confirmed
   chain history, verifies IPFS, and publishes the Kafka reference.
7. [`scoring_worker.py`](packages/integrations/kafka/scoring_worker.py) performs
   replay-safe feature processing, duplicate screening, XGBoost, SHAP, and chain
   write-back.

Read the module docstrings and comments around failure ordering carefully. In an
event-driven system, the order of persistence, publication, confirmation, and
offset commits is part of correctness, not just an implementation detail.

## Component responsibilities

The easiest way to understand the repository is to follow ownership. Each
component has one primary job and deliberately does not absorb the keys or
responsibilities of its neighbours.

| Component | Plain-English job | Technical boundary | Guide |
| --- | --- | --- | --- |
| Frontend | Collect a fictional claim and explain what is happening | React UI, claimant wallet proof, EIP-712 signature, polling and public receipt display | [Frontend](apps/frontend/README.md) |
| Backend | Check who may submit and prepare the exact sponsored call | FastAPI validation, short claimant sessions, policy eligibility, permit signing, canonical IPFS upload and durable preparation | [Backend](apps/backend/README.md) |
| Relayer | Pay the testnet gas without gaining claim authority | PostgreSQL outbox, EOA nonce allocation, raw-transaction persistence, fee replacement and receipt confirmation | [Relayer](apps/relayer/README.md) |
| Contracts | Enforce the public rules and retain the compact record | Solidity registry, EIP-712 permit verification, ERC-2771 sender recovery, role scopes and lifecycle transitions | [Contracts](apps/contracts/README.md) |
| Listener | Turn confirmed chain history into replayable application data | Confirmation-aware log polling, IPFS re-verification, PostgreSQL projection and Kafka publication | [Listener](apps/listener/README.md) |
| Kafka worker | Run slow screening outside the HTTP request | Versioned event validation, retry/quarantine policy, feature processing, scoring and chain write-back | [Kafka](packages/integrations/kafka/README.md) |
| PostgreSQL | Remember off-chain state that must survive retries | Migrations, idempotency/outbox state, chain index, immutable features, assessments and human-outcome revisions | [PostgreSQL](packages/integrations/postgres/README.md) |
| IPFS adapter | Store and retrieve the exact public claim bytes | Pinata upload, safe `ipfs://` parsing, gateway reads and byte-for-byte verification | [IPFS](packages/integrations/ipfs/README.md) |
| Duplicate detector | Find exact normalized incidents across configured insurers | Versioned HMAC fingerprinting and human-review match signals | [Duplicates](packages/duplicates/README.md) |
| Model | Produce a reproducible synthetic review signal | Leakage-controlled training, artifact verification, XGBoost inference and local SHAP reasons | [Model](packages/model/README.md) |
| Observability | Make long-running processes diagnosable without leaking secrets | Structured JSON logs, bounded Prometheus labels and graceful shutdown | [Observability](packages/observability/README.md) |
| GCP deployment | Run the research system as one reproducible cloud demonstration | Terraform, hardened single-VM Compose, monitoring and evidence collection | [GCP infrastructure](infrastructure/gcp/README.md) |

The [`packages/integrations` index](packages/integrations/README.md) explains
why Ethereum, IPFS, Kafka, and PostgreSQL are kept behind adapters instead of
being called directly from every application process.

## Small glossary

| Term | Meaning in this project |
| --- | --- |
| Anchor | A compact on-chain record proving the hash and pointer accepted for a claim. It is not the full claim |
| Canonical JSON | One deterministic byte representation, so every component calculates the same hash |
| CID | IPFS content identifier used as the public location-independent pointer |
| Claim permit | One-time, insurer-scoped EIP-712 authorization for an exact claim, claimant and deadline |
| Forward request | The exact sponsored contract call signed by the claimant or authorized representative |
| Gasless | The user signs but does not pay Sepolia gas. The restricted relayer pays to submit the unchanged call |
| Confirmation depth | Number of newer blocks the listener waits before treating an event as sufficiently stable |
| Projection | PostgreSQL's fast, rebuildable current view of confirmed contract events |
| Idempotent | Safe to repeat after timeout or crash without creating a second logical result |
| HMAC fingerprint | Keyed digest used for private equality checks. Unlike a plain hash, it resists easy offline guessing |
| SHAP reason | A model-specific explanation of which inputs moved one prediction. It is not proof of cause or fraud |

## Run the complete application locally

The complete application is not a single server. It has five long-running
application processes plus PostgreSQL and Kafka. Start them in the documented
order because a later process depends on the earlier infrastructure.

| Process | Needs | Main job |
| --- | --- | --- |
| FastAPI | PostgreSQL, Sepolia, Pinata, policy and permit configuration | Authenticate, validate, upload, and prepare an exact signed request |
| React/Vite | FastAPI and a browser wallet | Guide the user through submission and display results |
| Relayer | PostgreSQL, Sepolia RPC, funded test wallet | Pay testnet gas for an already authorized request |
| Listener | Sepolia RPC, IPFS, PostgreSQL, Kafka | Verify confirmed public events and publish references |
| Scoring worker | Kafka, IPFS, PostgreSQL, model, assessor wallet | Screen the claim and write the review status |

The detailed [local development guide](LOCAL_DEVELOPMENT.md) contains the full
credential separation, expected logs, recovery procedures, and shutdown steps.
The sequence below is the beginner-friendly checklist.

### 1. Complete the toolchain setup

Finish the [quick setup check](#quick-setup-check-no-secrets-required), then
install and start Docker Desktop or Docker Engine with Compose.

You also need all of the following for a real end-to-end test:

- a browser wallet such as MetaMask
- a working Sepolia RPC URL
- a Pinata JWT for uploading fictional JSON to public IPFS
- a claimant or representative wallet listed in the fictional policy record
- a permit-issuer key scoped to the policy's insurer
- a funded relayer wallet with no contract role
- an assessor wallet scoped to the policy's insurer
- access to the selected contract's configured roles, or permission to deploy
  and provision a new contract

Use dedicated test wallets that never hold real assets.

### 2. Create and understand `.env.local`

```bash
# Copy the example only when no local file exists. This protects existing keys.
test -f .env.local || cp .env.example .env.local

# Confirm Git ignores the secret file. Expected output includes .env.local.
git check-ignore .env.local
```

Do not load `.env.local` yet. Open it in a text editor and replace every local
placeholder relevant to your run. The file is grouped and heavily commented so
you can review one boundary at a time.

| Configuration group | Important settings | Beginner explanation |
| --- | --- | --- |
| Network | `SEPOLIA_RPC_URL`, `CLAIMS_DEPLOYMENT_ID`, `LISTENER_START_BLOCK` | Every process must read the same chain and contract deployment |
| Public storage | `PINATA_JWT`, `IPFS_GATEWAY` | FastAPI uploads the exact fictional JSON and other processes read it back |
| Claimant session | `CLAIMANT_*` secrets | Short-lived wallet login and privacy-preserving claimant identifiers |
| Policy and permit | `POLICY_ELIGIBILITY_RECORDS_JSON`, `CLAIM_PERMIT_ISSUERS_JSON` | Defines who can submit and which scoped key may authorize the claim |
| Gas relayer | `SEPOLIA_RELAYER_PRIVATE_KEY` or `_FILE` | Pays test ETH but has no authority to change claim contents |
| Scoring | `SEPOLIA_ASSESSOR_PRIVATE_KEY`, `XGBOOST_MODEL_DIR` | Allows only the reviewed model status and score update |
| Private equality checks | `CLAIM_AUTHORIZATION_KEY`, `DUPLICATE_FINGERPRINT_KEY`, `GASLESS_REQUEST_FINGERPRINT_KEY` | Independent HMAC keys for separate trust boundaries |
| Local services | `DATABASE_URL`, `KAFKA_*` | Connects the processes to the same PostgreSQL database and Kafka topic |

#### Checked-in deployment does not mean checked-in access

The repository includes the ABI, addresses, and deployment block for
`sepolia-public-intake-v1`. It intentionally does **not** include the private
keys that control its roles. The placeholder path in
`CLAIM_PERMIT_ISSUERS_JSON` is deliberately invalid.

You have two valid choices:

1. Use `sepolia-public-intake-v1` only if you already control its matching
   permit-issuer, assessor, claimant, and relayer test wallets.
2. Otherwise, [deploy your own permit-backed contract](apps/contracts/README.md#sepolia-deployment),
   save it under a new deployment ID, record its exact deployment block, and
   follow the [public-intake provisioning guide](docs/README.md).

FastAPI fails during startup when the selected deployment, policy, permit key,
and live contract roles do not agree. This fail-closed behavior is expected and
prevents an incorrectly configured service from sponsoring claims.

When a local key is stored in a file, use an absolute path and restrict access:

```bash
# Replace the example path with the real location of a fictional test key.
chmod 600 /absolute/path/to/northstar-permit-issuer.key
```

Never place a secret in a variable beginning with `VITE_`. Vite copies every
such value into browser JavaScript where anyone can read it.

### 3. Build the reviewed model artifact

```bash
# Download the pinned synthetic dataset, verify it, then train and save XGBoost.
apps/backend/.venv/bin/python \
  -m packages.model.train_xgboost --download
```

Expected output is a new ignored directory at
`packages/model/artifacts/xgboost-african-motor-v1/`. The first run may take a
few minutes. The scoring worker refuses to start with a missing or mismatched
artifact.

### 4. Start PostgreSQL and Kafka

```bash
# Start the database, Kafka broker, and Kafka browser UI in the background.
docker compose -f packages/integrations/kafka/compose.yml up -d

# Wait until the service status is healthy before continuing.
docker compose -f packages/integrations/kafka/compose.yml ps

# Export every value from .env.local into this terminal only.
set -a
source .env.local
set +a

# Apply every database migration, then verify the schema is current.
apps/backend/.venv/bin/python \
  -m packages.integrations.postgres.migrations upgrade
apps/backend/.venv/bin/python \
  -m packages.integrations.postgres.migrations check

# Create the exact deployment-scoped topic. Repeating this command is safe.
docker compose -f packages/integrations/kafka/compose.yml exec kafka \
  /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 \
  --create \
  --if-not-exists \
  --topic "$KAFKA_CLAIM_SUBMITTED_TOPIC" \
  --partitions 3 \
  --replication-factor 1
```

If either container is not healthy, fix it before opening the application
terminals. Kafka UI is available at <http://127.0.0.1:8081>.

### 5. Start the five application terminals

Open five terminal tabs and keep all five commands running. In the API,
relayer, listener, and worker tabs, first load the same configuration:

```bash
# Every application command assumes the repository root.
cd /path/to/Decentralized-Claims-Registry

# Export .env.local values to the process started from this tab.
set -a
source .env.local
set +a
```

Then run one application in each tab:

```bash
# Terminal A: start the API. It prepares calls but never pays for a transaction.
unset SEPOLIA_DEPLOYER_PRIVATE_KEY
unset SEPOLIA_ASSESSOR_PRIVATE_KEY
unset SEPOLIA_RELAYER_PRIVATE_KEY
unset SEPOLIA_RELAYER_PRIVATE_KEY_FILE
apps/backend/.venv/bin/python -m uvicorn \
  apps.backend.app.main:app \
  --reload \
  --host 127.0.0.1 \
  --port 8000
```

```bash
# Terminal B: expose only the two documented browser-safe values.
# Do not source the server secret file in this tab.
export VITE_API_BASE_URL="http://127.0.0.1:8000"
export VITE_IPFS_GATEWAY="https://gateway.pinata.cloud/ipfs"
npm --prefix apps/frontend run dev -- --host 127.0.0.1
```

```bash
# Terminal C: start the isolated gas-paying process.
unset SEPOLIA_DEPLOYER_PRIVATE_KEY
unset SEPOLIA_ASSESSOR_PRIVATE_KEY
apps/backend/.venv/bin/python -m apps.relayer.gasless_relayer
```

```bash
# Terminal D: start the read-only chain listener and local indexer.
unset SEPOLIA_DEPLOYER_PRIVATE_KEY
unset SEPOLIA_ASSESSOR_PRIVATE_KEY
unset SEPOLIA_RELAYER_PRIVATE_KEY
unset SEPOLIA_RELAYER_PRIVATE_KEY_FILE
apps/backend/.venv/bin/python -m apps.listener.claims_listener
```

```bash
# Terminal E: start duplicate screening, XGBoost, SHAP, and chain write-back.
unset SEPOLIA_DEPLOYER_PRIVATE_KEY
unset SEPOLIA_RELAYER_PRIVATE_KEY
unset SEPOLIA_RELAYER_PRIVATE_KEY_FILE
apps/backend/.venv/bin/python \
  -m packages.integrations.kafka.scoring_worker
```

The `unset` commands reduce accidental sharing of transaction wallet keys in a
local convenience setup. A hosted deployment must use separate process-level
secret injection so each service receives only the capabilities it needs. See the
[full terminal instructions](LOCAL_DEVELOPMENT.md#6-start-the-five-application-terminals)
for expected startup log messages.

### 6. Verify before submitting

Run these commands in a sixth terminal:

```bash
# Liveness answers whether FastAPI itself is running.
curl --fail --silent http://127.0.0.1:8000/health/live \
  | apps/backend/.venv/bin/python -m json.tool

# Readiness checks the database, chain, IPFS, model, and configuration.
curl --fail --silent http://127.0.0.1:8000/health/ready \
  | apps/backend/.venv/bin/python -m json.tool

# Confirm the browser will receive the intended chain and contract addresses.
curl --fail --silent http://127.0.0.1:8000/claims/gasless/config \
  | apps/backend/.venv/bin/python -m json.tool
```

Do not submit until readiness reports every dependency as `ok`. Then open:

- claims application at <http://127.0.0.1:5173>
- API documentation at <http://127.0.0.1:8000/docs>
- human assessor console at <http://127.0.0.1:5173/assessor>
- indexer operations at <http://127.0.0.1:5173/operations>
- Kafka UI at <http://127.0.0.1:8081>

Connect the wallet named in your fictional policy record. The wallet first
signs a readable ownership challenge, then signs the exact EIP-712 request. It
does not pay gas. A healthy submission progresses through:

```text
Browser:  wallet connected -> ownership proved -> request signed
API:      preparing -> prepared -> authorized
Relayer:  signed -> broadcast -> confirmed
Listener: IPFS verified -> Kafka event published -> checkpoint advanced
Worker:   features -> duplicate check -> score -> assessment confirmed
Browser:  public anchor -> review signals -> indexed current state
```

The operations page accepts the raw operations key. FastAPI stores only the
key's SHA-256 digest in `INDEXER_OPERATIONS_API_KEY_SHA256`. Restart FastAPI
after changing that digest.

### Common setup problems

| Symptom | Likely reason | Fix |
| --- | --- | --- |
| `No module named pytest` or `uvicorn` | A different Python environment is running | Use `apps/backend/.venv/bin/python -m ...` exactly |
| API stops during startup | Deployment, policy, or permit role does not match | Recheck the selected deployment and live role bindings |
| Readiness says PostgreSQL is unavailable | Containers are stopped or migrations are missing | Run the Compose and migration commands from step 4 |
| Listener cannot publish | The deployment-specific Kafka topic does not exist | Repeat the topic creation command from step 4 |
| Worker cannot load the model | Artifact is absent or its checksum changed | Repeat the model command from step 3 |
| Claim stays `authorized` | Relayer is stopped, unfunded, or disconnected | Check the relayer tab, database, RPC, and test ETH balance |
| Confirmed claim is absent from the dashboard | Listener is still waiting for confirmations or catching up | Check the listener tab and `/operations` block lag |
| Wallet or policy is rejected | Connected wallet does not match the configured fictional policy | Update the policy adapter or connect the authorized test wallet |

The [full troubleshooting table](LOCAL_DEVELOPMENT.md#troubleshooting) covers
additional credential, fee-cap, and recovery cases.

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

The separate `/assessor` console records one of `ConfirmedFraud`, `Legitimate`,
or `Inconclusive` in PostgreSQL after model screening. Corrections append a new
revision instead of overwriting history. These human outcomes do not change the
contract status: `Approved`/`Rejected` remain business lifecycle decisions and
are never converted automatically into fraud labels. `Inconclusive` is excluded
from binary-label eligibility, and the application performs no automatic model
retraining.

## Sepolia deployments

| Purpose                       | Deployment ID               | Registry                                                                                            | Forwarder                                                                                         | Start block |
| ----------------------------- | --------------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ----------- |
| Permit-backed public intake   | `sepolia-public-intake-v1`  | [`0xb64B...7dff`](https://sepolia.etherscan.io/address/0xb64BaB321e0Fb19b2295f8182D5A37bAf85F7dff) | [`0xeff6...0BD0`](https://sepolia.etherscan.io/address/0xeff61937C6a11236D87863e763c13cd7083f0BD0) | `11516697` |
| Previous gasless flow (no public permits) | `sepolia-gasless-v1` | [`0x5A7A...A300`](https://sepolia.etherscan.io/address/0x5A7A3e22843397f998823D0d58aBd2E1f4b2A300) | [`0x0e68...5F0`](https://sepolia.etherscan.io/address/0x0e68Ac27a344f454373604Eec3144c427661E5F0) | `11426492` |
| Read-only legacy history      | `sepolia-security-audit-v1` | [`0x2AbA...B78cB`](https://sepolia.etherscan.io/address/0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB) | None                                                                                              | `11377814`  |

All use Sepolia chain ID `11155111`. Runtime selection is explicit through
`CLAIMS_DEPLOYMENT_ID`. The listener checkpoint and projection are additionally
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

- IPFS content is public and unencrypted.
- Valid sponsorship quotas are durable in PostgreSQL. Early or invalid-credential
  abuse counters remain process-local and should be enforced at the edge in a
  multi-instance production deployment.
- The dashboard reads a confirmed-event PostgreSQL index and may lag the chain
  by the configured confirmation depth.
- Exact incident fingerprints produce review candidates, not proof of fraud.
- The synthetic model is an integration artifact, not a validated decision model.
- Insurers sign through browser test wallets. Relayer and assessor keys are
  process-level testnet signers, not managed signing infrastructure.
- Local and GCP Compose files use one Kafka broker and one PostgreSQL instance.

Before real claim data, the design would still need encryption before IPFS,
managed keys and signing, enterprise identity, malware and evidence controls,
managed replicated infrastructure, explicit deep-reorganization recovery,
retention rules, and a validated and monitored model.

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
| Event delivery and replay                         | [Kafka](packages/integrations/kafka/README.md)                                                 |
| Feature and assessment storage                    | [PostgreSQL](packages/integrations/postgres/README.md)                                         |
| Shared adapter boundaries                         | [Integrations](packages/integrations/README.md)                                                |
| Deployment artifact selection                     | [Ethereum integration](packages/integrations/ethereum/README.md)                               |
| Logs, metrics and graceful shutdown               | [Observability](packages/observability/README.md)                                              |
| Single-VM deployment                              | [Google Cloud](infrastructure/gcp/README.md)                                                   |

Stop local infrastructure without deleting its volumes:

```bash
docker compose -f packages/integrations/kafka/compose.yml down
```
