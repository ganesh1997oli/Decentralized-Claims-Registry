# Gasless transaction relayer

The relayer is the only application process allowed to hold the dedicated
gas-paying wallet. It drains insurer-authorized EIP-712 requests from PostgreSQL,
persists a signed EOA transaction before broadcasting it, and confirms the exact
`ClaimSubmitted` event after the configured safe depth.

It is intentionally not part of FastAPI. An HTTP retry must never allocate an
Ethereum EOA nonce or gain access to the wallet that pays gas.

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

See the [local development guide](../../docs/local-development.md) for complete
startup order and the [production gasless runbook](../../docs/production-gasless-transactions.md)
for deployment, monitoring, compromise, and rollback procedures.

