# Claim rate limiting and authorised test bypass

This document is the technical runbook for the claim-submission limits in the
FastAPI gateway. It explains the implementation, the exact configuration
required for an authorised performance-test credential, and the checks to run
before and after a test.

The bypass is an operator-controlled backend capability. It is deliberately not
exposed in the browser or through an administrator endpoint.

> Use fictional claims only. A successful preparation uploads the claim to
> public IPFS, and a later wallet authorization can make the relayer spend
> Sepolia ETH. Use the automated in-memory tests for sustained API load; use the
> live gasless path only for a small, explicitly funded end-to-end test.

## Implementation map

| Component | Technical responsibility |
| --- | --- |
| `apps/backend/app/submission_auth.py` | Parses credential records, authenticates API keys, applies counters, evaluates the dual bypass controls, and emits the bypass audit event |
| `apps/backend/app/gasless_service.py` | Passes authenticated exemption policy into durable sponsorship reservation |
| `packages/integrations/postgres/gasless_submission_repository.py` | Serializes per-insurer/client decisions and enforces sponsorship limits consistently across API replicas |
| `apps/backend/app/main.py` | Builds one cached `SubmissionBoundary`, converts boundary failures to HTTP responses, and records the master-switch state at startup |
| `apps/backend/tests/test_submission_auth.py` | Verifies normal limits, dual-control activation, fail-closed behaviour, invalid-attempt handling, UTC reset, and audit logging |
| `.env.example` | Documents safe local defaults |
| `infrastructure/gcp/compose.yml` | Passes the master switch into the API container only |
| `infrastructure/gcp/.env.gcp.example` | Documents the hosted deployment setting without enabling it |

## Limits applied by the gateway

Two layers run before IPFS upload or relay authorization:

1. `SubmissionBoundary.authorize_and_reserve` provides fast in-process abuse
   protection, including invalid API-key attempts; and
2. `PostgresGaslessSubmissionRepository.begin_preparation` serializes valid
   sponsorship reservations across API workers and survives restarts.

| Boundary | Key | Window | Default | Counts |
| --- | --- | --- | --- | --- |
| Source IP | Normalised client IP | Rolling 60 seconds | 20 attempts | Invalid credentials, authorisation failures, and normal authorised submissions |
| Insurer | `credentialId` | Rolling 60 seconds | 5 submissions | Normal authorised submissions |
| Daily quota | `credentialId` + UTC date | Until the next UTC midnight | 25 per credential record | Normal authorised submissions |

The rolling windows use timestamp queues. Before checking a queue, the gateway
removes entries that are at least 60 seconds old. If the queue is already full,
the request receives `429 Too Many Requests` and a calculated `Retry-After`
header.

The daily quota is read from each credential's `dailyQuota` value. It resets on
the first request after UTC midnight. A normal authorised request reserves the
IP allowance first and the insurer allowances second, so an attempt rejected by
an insurer limit still consumes one IP attempt.

The gateway's invalid-attempt and early authenticated counters live in process
memory and reset with FastAPI. The valid gasless sponsorship decision is checked
again under PostgreSQL advisory locks using durable rows, so multiple API
replicas cannot each grant the final quota slot. A production edge should still
add distributed invalid-credential throttling (for example at an API gateway or
Redis-backed boundary), because PostgreSQL intentionally never stores raw or
fingerprinted failed credentials.

## Bypass activation logic

Two independent controls must both be true:

1. The authenticated credential record contains `"rateLimitExempt": true`.
2. The API process starts with `ALLOW_RATE_LIMIT_BYPASS="true"`.

The resulting behaviour is:

| `rateLimitExempt` | `ALLOW_RATE_LIMIT_BYPASS` | Result |
| --- | --- | --- |
| `false` or omitted | `false` | Normal limits apply |
| `true` | `false` | Normal limits apply |
| `false` or omitted | `true` | Normal limits apply |
| `true` | `true` | Authenticated, authorised requests bypass the three counters |

This dual opt-in is fail-closed. Copying an exempt credential to another
environment does not activate it unless the environment switch is also changed.
Enabling the environment switch does not exempt ordinary credentials.

The bypass does not skip:

- API-key authentication;
- the credential's `permittedOperations` check;
- comparison of the authenticated insurer with the request's `insurerId`;
- request size and schema validation;
- IPFS upload and byte-for-byte verification;
- Sepolia role, transaction, and receipt checks; or
- downstream listener and scoring controls.

An invalid key or insurer mismatch never receives the exemption and continues
to consume the source-IP allowance. The browser has no bypass checkbox, query
parameter, or administrator route that can alter this decision.

## Credential configuration

Do not mark a normal demonstration credential as exempt. The bypass must use a
separate credential for a unique synthetic insurer.

### Local walkthrough

The safe `.env.example` intentionally does **not** include an exempt credential.
Create one for a dedicated fictional wallet whose address already holds
`SUBMITTER_ROLE` in the selected deployment:

```bash
apps/backend/.venv/bin/python \
  apps/backend/scripts/generate_insurer_credential.py \
  performance-test-insurer performance-test-v1 \
  0xYOUR_ROLE_AUTHORIZED_TEST_SIGNER \
  --daily-quota 25
```

The generator prints a raw key once and a digest-only JSON record. Add
`"rateLimitExempt": true` to that generated record before appending it to
`INSURER_CREDENTIALS_JSON`:

```json
{
  "credentialId": "performance-test-v1",
  "insurerId": "performance-test-insurer",
  "signerAddress": "0xYOUR_ROLE_AUTHORIZED_TEST_SIGNER",
  "apiKeySha256": "GENERATED_64_CHARACTER_SHA256_DIGEST",
  "dailyQuota": 25,
  "rateLimitExempt": true
}
```

The raw key cannot be recovered from the digest. Keep it outside source control.
If the performance insurer is not in the normal React selector, use a controlled
test client that implements the complete gasless protocol described below; a
direct `POST /claims` is disabled and returns HTTP 410.

### Hosted or independently secured test

Generate a new random credential from the repository root:

```bash
apps/backend/.venv/bin/python \
  apps/backend/scripts/generate_insurer_credential.py \
  performance-test-insurer performance-test-v1 \
  0xYOUR_ROLE_AUTHORIZED_TEST_SIGNER \
  --daily-quota 25
```

The command prints two values:

- a raw API key, which is supplied only to the test client; and
- a JSON record containing its SHA-256 digest, which is stored by the API.

The raw key cannot be recovered from the digest. Save it in a secure temporary
location when it is generated, do not commit it, and do not put it in a
browser-facing `VITE_` variable.

Add the exemption to the generated digest-only JSON record:

```json
{
  "credentialId": "performance-test-v1",
  "insurerId": "performance-test-insurer",
  "signerAddress": "0xYOUR_ROLE_AUTHORIZED_TEST_SIGNER",
  "apiKeySha256": "GENERATED_64_CHARACTER_SHA256_DIGEST",
  "dailyQuota": 25,
  "rateLimitExempt": true
}
```

Append that object to the existing `INSURER_CREDENTIALS_JSON` array in the
ignored hosted environment file, such as `infrastructure/gcp/.env.gcp`. The
parser accepts a JSON Boolean only: `true` is valid, while strings such as
`"true"`, `"yes"`, or `1` are rejected.
Credential IDs, insurer IDs, and API-key digests must be unique, and the current
prototype permits one active credential per insurer.

## Enable and run a controlled test

1. Confirm that `.env.local` contains a separately generated credential record
   with `"rateLimitExempt": true`, and that its `signerAddress` holds the
   submitter role.
2. In `.env.local`, change only the server-side master switch:

   ```bash
   ALLOW_RATE_LIMIT_BYPASS="true"
   ```

   Leave `.env.example` set to `false`; it is the safe template committed to
   source control.
3. Load the single local environment file in the API terminal:

   ```bash
   set -a
   source .env.local
   set +a
   ```

4. Start or restart FastAPI. A running process does not re-read environment
   changes:

   ```bash
   apps/backend/.venv/bin/python -m uvicorn \
     apps.backend.app.main:app \
     --host 127.0.0.1 \
     --port 8000
   ```

5. Confirm the structured startup event contains
   `"rate_limit_bypass_enabled":true`:

   ```text
   api.submission_boundary_configured
   ```

6. Run the complete protocol with the raw key and matching test wallet:

   1. `GET /claims/gasless/config` and select the returned chain;
   2. `POST /claims/gasless/prepare` with `X-Insurer-API-Key`,
      `X-Insurer-Signer-Address`, and a unique `Idempotency-Key`;
   3. sign the returned EIP-712 typed data with that insurer wallet;
   4. `POST /claims/gasless/{submission_id}/authorize` with the signature; and
   5. poll `GET /claims/gasless/{submission_id}` until a terminal state.

   Reuse an Idempotency-Key only when retrying the exact same claim. For a load
   test, automate the EIP-712 signer in a protected test harness; never put its
   private key in FastAPI or the React bundle.

7. Check for one warning event per bypassed request:

   ```text
   submission.rate_limit_bypassed
   ```

   The event contains the non-secret `insurer_id` and the bypass scope
   `ip,insurer_minute,daily_quota`. It does not record the raw API key, its
   digest, or the client IP.

For a hosted test, substitute the newly generated raw key, change the switch in
the ignored `infrastructure/gcp/.env.gcp` file, and restart the backend
container. View its API events with:

```bash
docker compose \
  --env-file infrastructure/gcp/.env.gcp \
  -f infrastructure/gcp/compose.yml \
  logs backend
```

## HTTP outcomes

| Status | Meaning | Relevant header |
| --- | --- | --- |
| `401` | The API key is absent or does not match a configured digest | `WWW-Authenticate: ApiKey` |
| `403` | The key is valid but cannot submit for the requested insurer or operation | None |
| `429` | A normal credential has reached an IP, insurer-minute, or daily boundary | `Retry-After` in seconds |
| `201` | Preparation is durable; EIP-712 wallet signature may be required | None |
| `202` | Wallet authorization is durable and queued for sponsorship | None |
| `200` | Status read; a confirmed response includes the public receipt | None |

A bypass removes only rate-limit `429` responses for a correctly authenticated
and authorised exempt credential. Upstream failures can still produce other
responses, and throughput remains bounded by IPFS, RPC, wallet nonce, and
transaction confirmation performance. In the gasless design the relayer, rather
than FastAPI, owns the payer nonce and broadcast rate.

## Verify the implementation without external writes

The focused automated tests use in-memory objects and do not upload to IPFS or
submit Sepolia transactions:

```bash
apps/backend/.venv/bin/python -m pytest \
  apps/backend/tests/test_submission_auth.py \
  apps/backend/tests/test_gasless_service.py -q
apps/backend/.venv/bin/python -m ruff check \
  apps/backend/app/submission_auth.py \
  apps/backend/app/gasless_service.py \
  apps/backend/tests/test_submission_auth.py
```

The tests verify all four activation-matrix combinations, invalid-key IP
protection, insurer mismatch handling, strict Boolean parsing, UTC quota reset,
and the structured audit event.

## Disable and clean up

After the controlled test:

1. Restore `ALLOW_RATE_LIMIT_BYPASS="false"` in `.env.local`.
2. Reload `.env.local`, restart FastAPI, and confirm the startup event reports
   `"rate_limit_bypass_enabled":false`.
3. Remove or rotate a generated test credential if it is no longer needed.
4. Delete any temporary file containing a generated hosted raw key.
5. Retain only redacted audit evidence required for the dissertation.

Disabling the master switch is sufficient to restore limits immediately after
the restart, even if the digest record still contains `rateLimitExempt: true`.
