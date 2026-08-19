# Gasless transaction relayer

The relayer is the only application process allowed to hold the dedicated
gas-paying wallet. It drains insurer-authorized EIP-712 requests from PostgreSQL,
persists a signed EOA transaction before broadcasting it, and confirms the exact
`ClaimSubmitted` event after the configured safe depth.

It is intentionally not part of FastAPI. An HTTP retry must never allocate an
Ethereum EOA nonce or gain access to the wallet that pays gas.

## Quick mental model

The relayer is a **restricted postage payer**. The claimant and insurer have
already authorized the exact envelope; the relayer may pay to deliver it, but
cannot change its contents or grant itself a registry role.

| Boundary | Relayer responsibility |
| --- | --- |
| Reads | Only durable `authorized` submissions from the PostgreSQL outbox |
| Verifies | Deployment, forwarder request, signatures, fee/gas caps, registry roles and matching receipt event |
| Owns | One dedicated Sepolia gas-paying key and its durable nonce/attempt history |
| Writes | Signed raw transaction before broadcast, attempt hashes, replacement fees and final receipt state |
| Must not own | Claimant, permit-issuer, assessor or admin authority; IPFS upload; model scoring |

Persisting the raw transaction before broadcast closes the most dangerous crash
window: after a restart, the process can resend the same bytes instead of
silently allocating a second nonce.

## State transition owned by the relayer

```mermaid
flowchart LR
    Authorized["authorized"] -->|"reserve nonce + persist raw tx"| Signed["signed"]
    Signed -->|"broadcast exact bytes"| Broadcast["broadcast"]
    Broadcast -->|"receipt below safe depth"| Broadcast
    Broadcast -->|"stuck; same nonce + higher fee"| Signed
    Broadcast -->|"matching event at safe depth"| Confirmed["confirmed"]
    Authorized -->|"expired/invalid"| Failed["failed or expired"]
    Signed -->|"terminal construction error"| Failed
    Broadcast -->|"revert/event mismatch"| Failed
```

The relayer treats fee-cap and dependency failures as retryable. Signature,
deployment, receipt-event, and revert mismatches are terminal because retrying
the same authorization cannot make them valid.

## Required configuration

| Setting | Meaning |
| --- | --- |
| `DATABASE_URL` | Durable outbox, nonce allocator and transaction attempts |
| `SEPOLIA_RPC_URL` | Sepolia reads, broadcast and receipts |
| `CLAIMS_DEPLOYMENT_ID` | Registry/forwarder artifact selected by every process |
| `SEPOLIA_RELAYER_PRIVATE_KEY` | Local-development key, or use the file setting |
| `SEPOLIA_RELAYER_PRIVATE_KEY_FILE` | Preferred secret-manager-mounted key file |
| `CONFIRMATION_BLOCKS` | Safe depth required before marking confirmed |
| `GASLESS_MAX_TRANSACTION_GAS` | Hard transaction gas ceiling |
| `GASLESS_MAX_FEE_GWEI` | Maximum total fee policy |
| `GASLESS_MAX_PRIORITY_FEE_GWEI` | Maximum tip policy |
| `GASLESS_STUCK_TRANSACTION_SECONDS` | Age before a reviewed same-nonce fee bump |
| `GASLESS_RELAY_POLL_SECONDS` | Delay between durable queue polls |

The account must:

- have enough Sepolia ETH for sponsored transactions;
- have no default-admin, submitter, or assessor role;
- be dedicated to this relayer deployment; and
- avoid unrelated manual transactions that consume its EOA nonce.

`GaslessRelayChain` verifies the absence of registry roles during startup. A
role-bearing relayer fails closed even though it could technically pay gas.

## Run locally

Start PostgreSQL and apply migration `005` before the relayer. From the
repository root:

```bash
set -a
source .env.local
set +a

apps/backend/.venv/bin/python -m apps.relayer.gasless_relayer
```

A healthy startup emits `gasless.relayer_started` with public addresses and
chain ID. It does not log the private key, raw insurer signature, or RPC URL.
When work arrives, expect:

```text
gasless.relay_broadcast
gasless.relay_confirmed
```

`gasless.relay_failed` includes a stable safe error code and exception type,
not provider response text that might include an authenticated RPC URL.

## Crash and retry behaviour

- Crash before signed bytes commit: no nonce is reserved; the next worker starts
  the transition again.
- Crash after signed bytes commit but before broadcast: the next worker broadcasts
  the exact stored transaction.
- RPC says “already known”: this is compatible with replaying the stored bytes;
  receipt polling continues.
- Broadcast has no receipt beyond the stuck threshold: a replacement uses the
  same nonce with a strictly higher fee, within configured caps.
- Crash after mining but before database confirmation: every stored attempt hash
  is checked, so the mined original or replacement is recovered.

Do not manually edit `gasless_relayer_nonces` or discard relay attempts. Pause
the relayer, inspect all stored hashes and the account's independent RPC history,
then follow the recovery procedure in the production runbook.

## Test

Unit tests use in-memory store/chain adapters and spend no test ETH:

```bash
apps/backend/.venv/bin/python -m pytest \
  apps/relayer/test_gasless_relayer.py -q
```

The PostgreSQL integration suite exercises durable nonce and replay behavior:

```bash
TEST_DATABASE_URL=postgresql://claims:claims-local@127.0.0.1:5432/claims_registry \
  apps/backend/.venv/bin/python -m pytest -m integration -q
```

See the [local development guide](../../LOCAL_DEVELOPMENT.md) for complete
startup order and the [production gasless runbook](README.md#production-gasless-claim-transactions)
for deployment, monitoring, compromise, and rollback procedures.

---

## Production gasless claim transactions

This design uses OpenZeppelin's `ERC2771Forwarder`, EIP-712 signatures, an
insurer-scoped claim permit, and a separate sponsoring relayer. The claimant or
authorized representative signs with their own wallet. FastAPI holds a narrowly
scoped permit-issuer key but no claimant or transaction-paying key, while the
relayer never receives a registry role.

> This makes the transaction path suitable for production hardening. It does
> not make the complete current application production-ready for real insurance
> data: it still uses Sepolia and public, unencrypted IPFS, and the custom
> contract needs an independent audit before mainnet use.

### Trust and process boundaries

```mermaid
sequenceDiagram
    participant W as Claimant wallet
    participant A as Transaction-keyless FastAPI
    participant P as PostgreSQL outbox
    participant R as Isolated relayer
    participant F as ERC2771Forwarder
    participant C as ClaimsRegistry

    W->>A: Wallet challenge + signature
    A-->>W: Short-lived claimant session
    W->>A: Claim + policy reference + Idempotency-Key
    A->>A: Verify claimant/delegate, coverage, amount, incident and quotas
    A->>A: Canonical schema-v6 claim + HMAC authorization
    A->>A: Pin and verify exact IPFS bytes
    A->>A: Sign insurer-scoped single-use ClaimPermit
    A->>F: Read wallet nonce and chain time
    A->>P: Persist exact call, nonce, gas, deadline
    A-->>W: EIP-712 ForwardRequest
    W->>W: Review and sign exact domain/message
    W->>A: 65-byte signature
    A->>F: verify(request, signature)
    A->>P: Durable authorized outbox state
    R->>P: Reserve EOA nonce and persist signed raw transaction
    R->>F: execute(request)
    F->>C: submitClaimWithPermit(parties, hash, pointer, permit)
    C-->>R: ClaimSubmitted + ClaimPartiesRecorded
    R->>P: Confirm exact parties and events after safe depth
    A-->>W: Durable receipt while browser polls
```

The interfaces are intentionally asymmetric:

- The browser has only the claimant/delegate wallet and a short-lived bearer
  session. It receives no insurer credential, permit key, or gas key.
- FastAPI can verify policy eligibility and issue scoped claim permits. Its
  permit key cannot pay for transactions, administer the registry, or assess a
  claim, and must be mounted from an owner-only secret file.
- The relayer has a capped gas wallet, but no admin, submitter, or assessor role.
- PostgreSQL contains signed authorizations and raw relay transactions. EIP-712
  domain, deadline, signer nonce, exact target, and exact calldata make them
  single-use and deployment-specific.
- `ClaimsRegistry` trusts one immutable forwarder and explicitly scopes each
  permit issuer to an insurer. Rotating the forwarder requires a new registry
  deployment and index migration; rotating a permit issuer is an admin action.

### Enforced invariants

The implementation fails closed on these conditions:

1. The selected deployment must contain both reviewed artifacts, live bytecode,
   the required ABIs, and a registry `trustedForwarder()` equal to the deployed
   forwarder address.
2. A one-time wallet challenge establishes the submitter. Policy eligibility
   then binds that submitter to a claimant, insurer, policy, coverage window,
   incident type, amount cap, and sponsorship quota without persisting the raw
   policy reference.
3. The claim permit fixes its deployment, permit ID, claim ID, claimant,
   submitter, insurer, claimant commitment, claim hash, pointer hash, and
   deadline. The recovered issuer must be actively scoped to that insurer and a
   permit ID can be consumed only once.
4. The signed forwarder domain fixes name `ClaimsRegistryForwarder`, version
   `1`, chain ID, and verifying contract. The request fixes registry target,
   zero value, capped forward gas, nonce, deadline, and
   `submitClaimWithPermit` calldata.
5. The API does not accept caller-selected target, function, gas, nonce, fees,
   or forwarder. `POST /claims` is permanently disabled with HTTP 410.
6. Idempotency keys are stored as HMACs and bound to an HMAC of the validated
   claim. Reusing a key with different content returns a conflict.
7. Valid sponsorship limits are enforced transactionally in PostgreSQL across
   API replicas. A ten-minute preparation lease releases crashed preparations;
   unsigned expired requests release the signer nonce.
8. One active request per stable wallet subject prevents two tabs from signing
   the same forwarder nonce. This deliberately serializes each submitter wallet;
   do not weaken the nonce invariant casually.
9. The relayer reserves EOA nonces under a database advisory lock, then persists
   signed bytes before broadcast. HTTP retries never allocate EOA nonces.
10. Stuck transactions are replaced at the same nonce with at least a 12.5% fee
   increase, bounded by configured gas and fee caps. Every original and
   replacement hash is retained because either can win the race into a block.
11. A public receipt is accepted only after the configured confirmation depth
    and exact `ClaimSubmitted` and `ClaimPartiesRecorded` events matching the
    stored permit, parties, hash, pointer, submitter, issuer, and deployment.
    The scoring worker repeats these authorization bindings before using IPFS.

### Durable state machine

```mermaid
stateDiagram-v2
    [*] --> preparing
    preparing --> prepared: IPFS round-trip + forward request saved
    preparing --> failed: error or lease expiry
    prepared --> authorized: wallet signature verified on-chain
    prepared --> expired: deadline reached
    authorized --> signed: relayer nonce and raw tx persisted
    authorized --> expired: deadline reached before signing
    signed --> broadcast: exact raw bytes accepted/already known
    broadcast --> signed: fee-bumped replacement persisted
    broadcast --> confirmed: exact event at safe depth
    authorized --> failed: invalid authorization
    signed --> failed: terminal construction/revert mismatch
    broadcast --> failed: terminal receipt mismatch or revert
```

`gasless_claim_submissions` is the user-facing state and outbox.
`gasless_relayer_nonces` is the durable next-nonce allocator.
`gasless_relay_attempts` retains every same-nonce transaction. Do not edit these
tables manually during an incident; pause the relayer and follow reconciliation
steps first.

### Deployment procedure

Deploying contracts is an external, funded action and is intentionally not
performed by this branch.

1. Obtain an independent review of `ClaimsRegistry.sol`,
   `ClaimsForwarder.sol`, tests, compiler settings, and the exact OpenZeppelin
   version in the lockfile. Build with the production compiler profile.
2. Create distinct accounts for default admin, each insurer-scoped permit
   issuer, every assessor, any retained legacy submitter, and the relayer. The
   relayer must have a deliberately capped balance and no registry role. Keep
   the admin offline or behind multisig governance.
3. Deploy `ClaimsRegistryModule`. Record chain ID, both addresses, deployment
   transaction/block, compiler metadata, source verification links, and artifact
   checksums under a new immutable `CLAIMS_DEPLOYMENT_ID` directory. Never point
   gasless writers at `sepolia-security-audit-v1`; it has no trusted forwarder.
4. From the admin account, call `setPermitIssuer(issuer, insurer, true)` for
   every configured insurer and `setAssessor(assessor, insurer, true)` for every
   assessment scope. Verify `isPermitIssuerFor`, `isAssessorFor`, role
   separation, `defaultAdmin`, and `trustedForwarder` through an independent RPC
   and block explorer. Use `setSubmitter` only for an explicitly retained legacy
   integration, never for public claimant wallets.
5. Apply all migrations through `008` before API or relayer rollout. Back up
   PostgreSQL and test restoration. A changed contract address is a new
   projection scope; set `LISTENER_START_BLOCK` to its exact deployment block
   and use new Kafka topic and consumer-group names so legacy events cannot be
   confused with the public-intake deployment.
6. Deploy the transaction-keyless API first with the relayer stopped. Verify
   readiness and `GET /claims/gasless/config`. Confirm the API environment
   contains no deployer, claimant, assessor, or relayer transaction key. Mount
   only the configured permit issuer's owner-only key file.
7. Deploy the relayer separately. For a production environment,
   `DEPLOYMENT_ENVIRONMENT=production` requires
   `SEPOLIA_RELAYER_PRIVATE_KEY_FILE`; mount it from a secret manager. Restrict
   process identity, filesystem, egress, and RPC credentials. A managed/HSM
   transaction signer is preferred when the platform supports Ethereum
   secp256k1 transaction signing.
8. Submit low-value canaries with an eligible claimant wallet and an authorized
   representative. Verify both browser signatures, every database transition,
   the permit issuer, the forwarder call, emitted parties, confirmation depth,
   listener projection, and worker authorization binding.
9. Enable traffic gradually. Keep the legacy direct POST disabled. Do not run
   both custodial and wallet-signed writers against the same operational path.

The checked-in `infrastructure/gcp/compose.yml` remains a single-VM research
topology. It demonstrates process-level key separation, but its single
PostgreSQL node, Kafka broker, RPC endpoint, and host are not a production HA
deployment. Production should use replicated managed data services, multi-zone
compute, TLS/service identity, secret-manager mounts, controlled migrations,
and at least two independent RPC paths.

### Configuration policy

API-only settings include claimant session/HMAC keys,
`POLICY_ELIGIBILITY_RECORDS_JSON`, `POLICY_REFERENCE_LOOKUP_KEY`,
`CLAIMANT_COMMITMENT_KEY`, `CLAIM_PERMIT_PRIVATE_KEY_FILE`,
`GASLESS_REQUEST_FINGERPRINT_KEY`, `CLAIM_AUTHORIZATION_KEY`, `PINATA_JWT`, and
the forward gas/TTL caps. Relayer-only settings include its private-key file,
transaction gas cap, fee caps, stuck threshold, and poll interval. Both receive
only read RPC configuration, deployment ID, and PostgreSQL access appropriate
to their process.

Recommended starting caps are intentionally conservative and must be load- and
fee-tested for the target network:

- forward request gas: 400,000; hard application maximum: 500,000;
- relay transaction gas: 600,000; hard application maximum: 750,000;
- signature TTL: 600 seconds; hard application maximum: 3,600 seconds;
- stuck threshold: 120 seconds;
- confirmations: 12;
- max fee: 100 gwei and max priority fee: 3 gwei on Sepolia examples.

A fee-cap breach is retryable and visible as `fee_cap_exceeded`; it is not a
reason to spend without bound. Changing caps is an operational change requiring
approval and budget review.

### Abuse and compromise consequences

| Compromise or failure | Consequence and containment |
| --- | --- |
| Claimant session stolen | The token expires quickly and cannot produce the required EIP-712 wallet signature. Re-authenticate, inspect the claimant's outbox rows, and rotate session keys if theft is systemic. |
| Claimant/delegate wallet stolen | The attacker can authorize claims for policies mapped to that wallet. Disable that wallet in the eligibility source, preserve events, and follow the wallet-compromise procedure. |
| Permit-issuer key stolen | The attacker can issue party bindings for every insurer in that key's active scopes. Revoke all scopes on-chain, stop public writes, rotate the key, and audit all permit events. |
| FastAPI compromised | The attacker can access policy configuration, pin data, issue scoped permits, and deny service, but cannot sign as a claimant or spend the relayer EOA directly. Revoke permit scopes, rotate API/HMAC/Pinata secrets, and inspect outbox rows. |
| PostgreSQL compromised | Stored exact authorizations can be relayed once before deadline, but cannot be changed or replayed across nonce/domain. Treat claim contents and identifiers as exposed. |
| Relayer key stolen | Attacker can drain only the funded relayer EOA; it has no registry role. Pause funding, stop relayers, rotate to a new dedicated account, and reconcile EOA nonces. |
| RPC lies or is partitioned | Readiness fails or transactions pause. Receipt event checks prevent a wrong claim from becoming confirmed locally. Compare with an independent RPC before repair. |
| Unknown EOA nonce use | `relayer_nonce_conflict` is retained as retryable and blocks silent skipping. Pause, inspect all attempts and chain transactions, then rotate/reconcile rather than editing the nonce table. |
| Fee spike | Signing/replacement pauses at the configured cap. Alert operators; do not auto-raise the budget. |
| Browser closes | An already authorized transaction continues from PostgreSQL. Re-authenticating the same wallet recreates the stable subject needed to read a known submission ID. |
| Forwarder vulnerability | Stop API and relayer immediately. Because trust is immutable, deploy a new registry/forwarder pair and migrate projection scope. |

Malformed requests and abusive challenge traffic should also be rejected at a
WAF or API gateway. Application limits and sponsorship accounting are durable
in PostgreSQL, but they are not a substitute for distributed denial-of-service
controls at the edge.

### Monitoring and incident thresholds

Alert on:

- any `failed` or `relayer_nonce_conflict` row;
- `authorized` older than one poll interval plus RPC tolerance;
- `signed` older than 30 seconds;
- `broadcast` without a receipt past `GASLESS_STUCK_TRANSACTION_SECONDS`;
- replacement count growth, fee-cap pauses, relayer balance runway, RPC error
  rate, database migration drift, and confirmation lag;
- registry role changes, default-admin transfer events, and any transaction sent
  directly from the relayer to a target other than the forwarder.

Dashboard status is not proof of finality. Use the stored block/hash and an
independent RPC during incident response. PostgreSQL backups must include all
three gasless tables consistently.

### Rollback and recovery

Stopping the relayer is the safe emergency brake; it prevents further spending
without invalidating already signed requests. Stop the prepare endpoint as well
when contract integrity or credential binding is in doubt. Confirmed blockchain
transactions cannot be rolled back. Prepared/authorized requests expire by
deadline; signed/broadcast requests must be reconciled against every stored hash
before any nonce repair.

Application rollback must preserve migration `005` and understand all states.
Never deploy an older API that falls back to the former backend submitter key.
For a contract rollback, treat the previous deployment as read-only history and
deploy a new version with a new deployment ID, listener start block, topic, and
projection scope.
