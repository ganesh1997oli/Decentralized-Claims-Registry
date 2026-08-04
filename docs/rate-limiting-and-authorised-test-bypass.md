# Claim rate limiting and authorised test bypass

This document is the technical runbook for the claim-submission limits in the
FastAPI gateway. It explains the implementation, the exact configuration
required for an authorised performance-test credential, and the checks to run
before and after a test.

The bypass is an operator-controlled backend capability. It is deliberately not
exposed in the browser or through an administrator endpoint.

> Use fictional claims only. A successful API request uploads the claim to
> public IPFS and submits a Sepolia transaction. Use mocked adapters or a local
> chain for sustained performance testing.

## Implementation map

| Component | Technical responsibility |
| --- | --- |
| `apps/backend/app/submission_auth.py` | Parses credential records, authenticates API keys, applies counters, evaluates the dual bypass controls, and emits the bypass audit event |
| `apps/backend/app/main.py` | Builds one cached `SubmissionBoundary`, converts boundary failures to HTTP responses, and records the master-switch state at startup |
| `apps/backend/tests/test_submission_auth.py` | Verifies normal limits, dual-control activation, fail-closed behaviour, invalid-attempt handling, UTC reset, and audit logging |
| `.env.example` | Documents safe local defaults |
| `infrastructure/gcp/compose.yml` | Passes the master switch into the API container only |
| `infrastructure/gcp/.env.gcp.example` | Documents the hosted deployment setting without enabling it |

## Limits applied by the gateway

The counters are checked inside `SubmissionBoundary.authorize_and_reserve`
before the service uploads to IPFS or sends a blockchain transaction.

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

These counters live in process memory. They reset when FastAPI restarts and are
not shared between multiple API workers or hosts. This is suitable for the
single-process dissertation prototype. A multi-instance deployment would need
an atomic shared implementation, such as Redis, plus load testing to establish
production thresholds.

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

The consolidated `.env.example` already contains this dedicated digest-only
record inside `INSURER_CREDENTIALS_JSON`:

```json
{
  "credentialId": "performance-test-v1",
  "insurerId": "performance-test-insurer",
  "apiKeySha256": "a046fd6eea194db30bba3f5deb7a6d9fc5b7b7f62ac6009f539052509bae3036",
  "dailyQuota": 25,
  "rateLimitExempt": true
}
```

Its corresponding fictional local raw key is:

```text
local-performance-test-insurer-api-key-change-me
```

Copying `.env.example` to `.env.local` therefore provides all configuration in
one file. No rate-limit override file is required. The raw key is intentionally
public for local fictional-data testing and must not be reused in a hosted
environment.

The performance-test insurer is not shown in the normal React insurer selector.
Use `curl`, an API client, or the FastAPI documentation at
`http://127.0.0.1:8000/docs` for the controlled test.

### Hosted or independently secured test

Generate a new random credential from the repository root:

```bash
python apps/backend/scripts/generate_insurer_credential.py \
  performance-test-insurer performance-test-v1 \
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

1. Confirm that `.env.local` contains the dedicated credential record with
   `"rateLimitExempt": true`. A local file copied from the current
   `.env.example` already contains it.
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
   python -m uvicorn apps.backend.app.main:app \
     --host 127.0.0.1 \
     --port 8000
   ```

5. Confirm the structured startup event contains
   `"rate_limit_bypass_enabled":true`:

   ```text
   api.submission_boundary_configured
   ```

6. Send local claims with the dedicated raw key in `X-Insurer-API-Key` and use
   the matching synthetic insurer ID in the JSON body:

   ```bash
   curl http://127.0.0.1:8000/claims \
     -H 'Content-Type: application/json' \
     -H 'X-Insurer-API-Key: local-performance-test-insurer-api-key-change-me' \
     -d '{
       "insurerId": "performance-test-insurer",
       "claimReference": "load-test-0001",
       "policyReference": "synthetic-load-policy-0001",
       "claimType": "collision",
       "incidentDate": "2026-08-04",
       "claimAmountUsd": 2500,
       "policyPremiumUsd": 480,
       "vehicleAge": 6,
       "vehicleType": "sedan",
       "country": "Nigeria",
       "regionType": "urban",
       "thirdPartyInjuryFlag": false,
       "totalLossFlag": false,
       "description": "Fictional performance-test claim",
       "evidence": []
     }'
   ```

   Use a new `claimReference` for each request. Repeating references can exercise
   duplicate or contract behaviour instead of measuring a clean submission.

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
| `201` | IPFS verification and the Sepolia anchor both completed | None |

A bypass removes only rate-limit `429` responses for a correctly authenticated
and authorised exempt credential. Upstream failures can still produce other
responses, and throughput remains bounded by IPFS, RPC, wallet nonce, and
transaction confirmation performance.

## Verify the implementation without external writes

The focused automated tests use in-memory objects and do not upload to IPFS or
submit Sepolia transactions:

```bash
source apps/backend/.venv/bin/activate
python -m pytest apps/backend/tests/test_submission_auth.py -q
ruff check apps/backend/app/submission_auth.py \
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
3. Remove or rotate a generated hosted test credential if it is no longer
   needed. The public local example may remain because the master switch is
   disabled.
4. Delete any temporary file containing a generated hosted raw key.
5. Retain only redacted audit evidence required for the dissertation.

Disabling the master switch is sufficient to restore limits immediately after
the restart, even if the digest record still contains `rateLimitExempt: true`.
