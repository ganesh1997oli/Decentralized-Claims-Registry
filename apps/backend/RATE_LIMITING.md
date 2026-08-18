# Public claim intake limits

The API has two durable abuse-control boundaries: wallet authentication and
sponsored claim preparation. Both use PostgreSQL transactions so limits remain
consistent across API replicas and restarts.

> Use fictional claim data in this repository. Preparing a claim uploads the
> canonical document to public IPFS, and authorizing it can spend Sepolia ETH
> from the relayer account.

## Boundaries

| Boundary | Durable key | Default | Configuration |
| --- | --- | ---: | --- |
| Challenge requests by client | HMAC client fingerprint | 20/minute | `CLAIMANT_AUTH_CLIENT_RATE_PER_MINUTE` |
| Challenge requests by wallet | Wallet address | 5/minute | `CLAIMANT_AUTH_WALLET_RATE_PER_MINUTE` |
| Sponsored preparations by claimant | HMAC wallet subject | 5/minute | `CLAIMANT_RATE_LIMIT_PER_MINUTE` |
| Sponsored preparations by client | HMAC client fingerprint | 20/minute | `IP_RATE_LIMIT_PER_MINUTE` |
| Sponsored preparations by policy | HMAC wallet subject + UTC day | Policy-specific | `dailyQuota` in `POLICY_ELIGIBILITY_RECORDS_JSON` |
| Concurrent sponsored work | HMAC wallet subject | One active submission | Fixed state-machine invariant |

The API returns `429 Too Many Requests` with a positive `Retry-After` header
when a configurable limit is exhausted. A second active submission returns a
conflict until the existing workflow reaches a terminal state or its unsigned
preparation lease expires.

## Authentication challenges

`POST /claimant/session/challenge` stores the exact readable message the wallet
will sign. Before inserting it, the repository serializes the relevant client
and wallet decisions, counts challenges issued in the previous minute, and
rejects requests that exceed either limit.

Client identifiers are stored as domain-separated HMAC fingerprints. Raw IP
addresses are never written to the challenge table. Wallet addresses are kept
because signature verification must bind the recovered signer to the original
request. A challenge is valid once, expires after
`CLAIMANT_CHALLENGE_TTL_SECONDS`, and is atomically consumed when a session is
created. Expired records are retained briefly as audit and rate-limit evidence,
then removed opportunistically.

## Sponsored submissions

`POST /claims/gasless/prepare` authenticates the short-lived claimant bearer
session, verifies policy eligibility, then asks PostgreSQL to reserve
sponsorship before any paid IPFS or chain work occurs. Advisory locks make the
following decisions atomic across replicas:

- idempotency-key reuse is allowed only for the same canonical claim;
- one claimant cannot hold multiple active forwarder requests;
- per-minute claimant and client limits cannot be over-issued; and
- the policy's UTC daily sponsorship quota cannot be over-issued.

The bearer token is never stored. Submission ownership uses a stable,
environment-specific HMAC subject derived from the authenticated wallet. The
client address is also HMAC-fingerprinted before persistence.

## Controlled performance-test bypass

`ALLOW_RATE_LIMIT_BYPASS` defaults to `false` and must remain disabled in normal
environments. The bypass affects sponsored preparation limits only; it never
skips wallet authentication, policy eligibility, idempotency, one-active-claim
protection, IPFS verification, contract roles, signatures, receipt validation,
or downstream processing.

Public policy records are not exempt by configuration, so enabling the master
switch alone does nothing for claimant traffic. A test-only caller must be
constructed explicitly with `rate_limit_exempt=True` inside an isolated test
harness. Do not add a browser flag, query parameter, API route, or policy JSON
field that can activate this behavior.

For routine load tests, replace IPFS, RPC, and relayer adapters with the
repository's in-memory/test doubles. Use the live Sepolia path only for a small,
funded end-to-end verification.

## Operational checks

Before enabling public submission, confirm:

1. PostgreSQL migrations `007` and `008` are applied.
2. All HMAC/session keys contain independent, high-entropy production values.
3. The policy eligibility source contains the expected claimant and delegate
   wallets and an intentionally chosen `dailyQuota`.
4. Edge infrastructure also limits request bodies, connections, and obviously
   abusive unauthenticated traffic.
5. Metrics alert on sustained HTTP 429 responses, active-preparation conflicts,
   relay failures, and relayer balance.

The application-level controls protect business invariants. They complement,
but do not replace, distributed edge throttling and denial-of-service controls.

## Test coverage

The relevant suites are:

- `apps/backend/tests/test_claimant_auth.py` for one-time challenges, signature
  recovery, token expiry, and challenge limits;
- `apps/backend/tests/test_policy_eligibility.py` for claimant/delegate,
  coverage, incident, amount, and quota policy;
- `apps/backend/tests/test_gasless_service.py` for public preparation and
  durable workflow behavior; and
- `packages/integrations/postgres/tests/test_migrations.py` for the persistence
  schema and constraints.
