# FastAPI backend

The backend is a keyless preparation and status service. It authenticates the
insurer, binds the credential to its wallet, creates and verifies one canonical
IPFS document, and returns an exact EIP-712 request. A separate relayer sponsors
only wallet-authorized requests from the PostgreSQL outbox.

> Claim content is public and unencrypted on IPFS. Use fictional test data only.

## Submission flow

```mermaid
sequenceDiagram
    participant B as Browser
    participant A as FastAPI
    participant I as IPFS / Pinata
    participant P as PostgreSQL outbox
    participant R as Relayer
    participant E as Forwarder + registry

    B->>A: API key + wallet + Idempotency-Key
    A->>A: Bind credential to wallet; reserve durable quota
    A->>A: Validate and authorize canonical schema-v5 JSON
    A->>I: Upload exact bytes
    A->>I: Download and compare exact bytes
    A->>P: Store exact ForwardRequest
    A-->>B: EIP-712 typed data
    B->>B: Insurer wallet signs
    B->>A: POST signature
    A->>E: Verify signature
    A->>P: Authorized outbox state
    R->>P: Persist raw sponsored transaction
    R->>E: execute(ForwardRequest)
    A-->>B: Poll durable confirmed receipt
```

The API stops after durable authorization; the isolated relayer creates the
permanent anchor. The listener and Kafka worker own duplicate screening,
feature persistence, XGBoost/SHAP, and assessment write-back.

## Code map

| File | Responsibility |
| --- | --- |
| `app/main.py` | Routes, dependencies, CORS, liveness and error translation |
| `app/models.py` | Strict request, IPFS document and response shapes |
| `app/submission_auth.py` | Digest-based credentials, quotas, request size and HMAC attestation |
| `app/gasless_service.py` | Idempotent IPFS, EIP-712, sponsorship and status workflow |
| `app/gasless_blockchain.py` | Keyless preparation plus least-privilege relay adapter |
| `apps/relayer/gasless_relayer.py` | Separate durable sign/broadcast/confirm worker |
| `app/health.py` | Dependency-safe readiness reporting |

The query service reads the deployment-scoped PostgreSQL projection. Loading the
dashboard does not construct a Web3 client, wallet, or Pinata upload client.

## Endpoints

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/health` | Compatibility alias for liveness |
| `GET` | `/health/live` | Confirms that the process can answer HTTP |
| `GET` | `/health/ready` | Checks auth config, migrations, IPFS signing config and Sepolia access |
| `GET` | `/claims?page=1&page_size=10` | Confirmed indexed state, newest first; maximum page size 50 |
| `GET` | `/claims/{claim_id}/assessment` | Stored model and duplicate result, or `404` while pending |
| `GET` | `/claims/gasless/config` | Public server-authoritative wallet network preflight |
| `POST` | `/claims/gasless/prepare` | Authenticate, upload, and return exact EIP-712 typed data |
| `POST` | `/claims/gasless/{id}/authorize` | Verify wallet signature and enqueue sponsorship |
| `GET` | `/claims/gasless/{id}` | Read durable relay/confirmation status for the owning credential |
| `POST` | `/claims` | Disabled with HTTP 410; no custodial fallback |

`201` means a wallet-signable request was prepared, not that it was mined.
Only a `confirmed` polling response contains the anchor receipt;
`assessment: null` then means asynchronous screening has not completed yet.

## Run locally

The complete startup order is documented in the
[local development guide](../../docs/local-development.md). For the backend
alone, run from the repository root after PostgreSQL is healthy and migrations
are current:

```bash
test -f .env.local || cp .env.example .env.local
set -a
source .env.local
set +a

# The API must remain keyless even though the shared local file also contains
# worker/relayer settings for developer convenience.
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

`--reload` is a development convenience: Uvicorn starts a watcher and replaces
the application process when Python files change. It does not reload values in
`.env.local` automatically; stop and restart the command after changing a
credential digest, deployment ID, or other environment setting.

Verify the process and its dependencies separately:

```bash
curl --fail --silent http://127.0.0.1:8000/health/live \
  | apps/backend/.venv/bin/python -m json.tool
curl --fail --silent http://127.0.0.1:8000/health/ready \
  | apps/backend/.venv/bin/python -m json.tool
curl --fail --silent http://127.0.0.1:8000/claims/gasless/config \
  | apps/backend/.venv/bin/python -m json.tool
```

Liveness only proves that FastAPI can answer HTTP. Readiness also checks insurer
configuration, current migrations, the gasless contracts and submitter roles,
Pinata configuration, claim authorization, and the operations credential.

Useful URLs:

- Liveness: <http://127.0.0.1:8000/health/live>
- Readiness: <http://127.0.0.1:8000/health/ready>
- OpenAPI UI: <http://127.0.0.1:8000/docs>
- Indexer operations API: <http://127.0.0.1:8000/operations/indexer>
- Indexer event search: <http://127.0.0.1:8000/operations/indexer/events>

## Configuration boundaries

| Setting | Used by | Meaning |
| --- | --- | --- |
| `INSURER_CREDENTIALS_JSON` | API | Credential IDs, insurer IDs, unique wallet signer addresses, SHA-256 digests, quotas and optional test exemptions |
| `CLAIM_AUTHORIZATION_KEY` | API + worker | Signs the canonical claim so the worker can trust its insurer identity |
| `GASLESS_REQUEST_FINGERPRINT_KEY` | API | HMACs idempotency content and client fingerprints stored in PostgreSQL |
| `PINATA_JWT` | API | Server-side public upload credential |
| `DATABASE_URL` | API + relayer | Durable idempotency, relay outbox, index, assessments and duplicate results |
| `SEPOLIA_RELAYER_PRIVATE_KEY_FILE` | Relayer only | Dedicated gas-paying account; forbidden from all registry roles |
| `FRONTEND_ORIGINS` | API | Allowed browser origins |
| `MAX_CLAIM_BODY_BYTES` | API | Request limit; default 16 KiB |
| `INSURER_RATE_LIMIT_PER_MINUTE` | API | Per-insurer submission limit; default 5 |
| `IP_RATE_LIMIT_PER_MINUTE` | API | Per-IP authentication-attempt limit; default 20 |
| `ALLOW_RATE_LIMIT_BYPASS` | API | Master switch for explicitly exempt performance-test credentials; default `false` |
| `INDEXER_OPERATIONS_API_KEY_SHA256` | API | SHA-256 digest of the separate read-only operations credential |
| `INDEXER_STALE_AFTER_SECONDS` | API | Marks a lagging checkpoint stalled after this age; default 120 seconds |
| `CONFIRMATION_BLOCKS` | API + listener | Keeps the displayed safe head consistent with listener confirmation depth |

The deployer, relayer, assessor, and insurer wallet keys do not belong in the API
container. Never put any secret in a `VITE_` variable. See the
[production gasless runbook](../../docs/production-gasless-transactions.md) for
deployment, limits, replacements, monitoring, compromise, and rollback.

### Indexer operations authentication

The dedicated `/operations/indexer` and `/operations/indexer/events` endpoints
require `X-Operations-API-Key`. The first returns bounded deployment telemetry;
the second provides filtered keyset pagination over immutable events without an
RPC request. Neither exposes a repair, reset, replay, or mutation action.
Generate a hosted credential with:

```bash
apps/backend/.venv/bin/python \
  apps/backend/scripts/generate_operations_credential.py
```

Give the raw value to trusted operators and store only the printed
`INDEXER_OPERATIONS_API_KEY_SHA256` value in API configuration. The browser keeps
the raw key in `sessionStorage`, which clears when that browser tab is closed;
it is never bundled into frontend JavaScript or sent in a URL. Put the route
behind an enterprise identity-aware proxy when one is available.

### Local insurer credentials

The example configuration contains one intentionally public local credential:

| Fictional insurer | Local key |
| --- | --- |
| `northstar-mutual` | `local-northstar-mutual-api-key-change-me` |

Its configured signer is the initial submitter in `sepolia-gasless-v1`. To use
another fictional insurer, generate a credential bound to that insurer wallet,
grant the wallet the on-chain submitter role, and grant the assessor scope for
that signer. FastAPI readiness checks every configured signer; leaving a
placeholder address in the JSON intentionally prevents the API from becoming
ready.

For a hosted research run, generate a random key and its digest-only record:

```bash
apps/backend/.venv/bin/python \
  apps/backend/scripts/generate_insurer_credential.py \
  northstar-mutual northstar-cloud-v1 0xYOUR_INSURER_WALLET \
  --daily-quota 25
```

The raw key is shown once for the fictional insurer operator. Put only the
printed JSON record in `INSURER_CREDENTIALS_JSON`. Valid sponsorship quotas are
rechecked durably under PostgreSQL locks; the earlier invalid-credential abuse
counter remains process-local and should also be enforced at a production edge.

### Authorised performance-test bypass

The normal claim form has no control that disables submission limits. For an
isolated performance test, create a dedicated credential for a test wallet that
has the required on-chain role. Its digest-only record must contain the explicit
exemption:

```json
{
  "credentialId": "performance-test-v1",
  "insurerId": "performance-test-insurer",
  "apiKeySha256": "a046fd6eea194db30bba3f5deb7a6d9fc5b7b7f62ac6009f539052509bae3036",
  "dailyQuota": 25,
  "rateLimitExempt": true
}
```

The corresponding public local key is
`local-performance-test-insurer-api-key-change-me`. Generate a new random
dedicated credential instead of using this example key for any hosted test.

The exemption activates only when the API also starts with
`ALLOW_RATE_LIMIT_BYPASS="true"`. Both controls are required deliberately: an
exempt credential behaves like a normal limited credential while the master
switch is false. Normal credentials remain limited when the switch is true, and
invalid or unauthorised attempts continue to count against the IP boundary.

Every successful bypass emits a structured `submission.rate_limit_bypassed`
audit event without logging the raw key or digest. The bypass skips only the
process-local IP, per-minute and daily counters; authentication, insurer
matching, request validation, IPFS verification and blockchain role checks
still apply. Disable the switch immediately after testing. Prefer a local chain
and mock IPFS for load tests because Sepolia transactions and public IPFS still
consume shared resources.

See the dedicated
[rate-limiting and authorised test-bypass runbook](../../docs/rate-limiting-and-authorised-test-bypass.md)
for the counter algorithm, activation matrix, operator procedure, audit fields,
HTTP outcomes, verification commands, and cleanup steps.

## Trying the API

Start with the public preflight endpoint:

```bash
curl --fail --silent http://127.0.0.1:8000/claims/gasless/config \
  | apps/backend/.venv/bin/python -m json.tool
```

The normal end-to-end client is the React form because preparation requires a
credential-bound wallet address and authorization requires an EIP-712 signature.
Swagger can inspect the request/response schemas, but it cannot safely replace
the wallet-signing step. Open <http://127.0.0.1:5173>, connect the configured
test wallet, and follow the browser progress states.

`POST /claims` is intentionally disabled and returns HTTP 410. There is no
server-custodial fallback that silently uses a backend submitter key.

## Failure behaviour

```mermaid
flowchart TD
    Request["POST /claims/gasless/prepare"] --> Auth{"Credential bound to wallet and within quota?"}
    Auth -->|No| FourXX["401 / 403 / 429"]
    Auth -->|Yes| Config{"Dependencies configured?"}
    Config -->|No| Unavailable["503 with safe JSON detail"]
    Config -->|Yes| Prepare["IPFS round trip + durable EIP-712 request"]
    Prepare -->|Fails| Gateway["409 / 502; no authorization queued"]
    Prepare -->|Succeeds| Created["201; wallet signature required"]
```

Readiness logs the dependency and exception type while public responses omit
connection strings, credentials, and upstream response bodies.

## Test

```bash
apps/backend/.venv/bin/python -m pytest apps/backend/tests -q
apps/backend/.venv/bin/python -m ruff check \
  apps/backend packages/duplicates packages/integrations
```

The isolated tests use in-memory adapters; they do not spend test ETH, upload to
Pinata, or require PostgreSQL.

## Known limits

- Public IPFS cannot protect real claim data.
- Claims pagination is served by the confirmed PostgreSQL event projection and
  can lag Sepolia by the configured confirmation depth.
- API keys and process-local invalid-attempt limits are not enterprise identity
  or distributed edge abuse prevention. Valid sponsorship quotas are durable.
- The relayer key should use a secret-manager file mount or managed signer and a
  capped balance. It never belongs in the API process.

See the [root runbook](../../README.md) and the
[Kafka worker guide](../../packages/integrations/kafka/README.md).
