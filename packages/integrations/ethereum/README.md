# Ethereum deployment integration

This adapter gives every Python process one shared way to select and
validate a checked-in `ClaimsRegistry` deployment. The environment chooses a
deployment ID; the adapter resolves its Ignition addresses and ABIs.

## Quick mental model

It is the **deployment guardrail** between local files and a live RPC endpoint.
Without it, the API, listener, relayer, and worker could accidentally use
different addresses or a legacy ABI while appearing individually healthy.

| Stage | Checks |
| --- | --- |
| Load | Deployment ID is present and path-safe; address/artifact files parse; required functions and events exist |
| Gasless requirement | Registry exposes the scoped assessor/forwarder interface and the forwarder exposes EIP-712 verification/execute functions |
| Public-intake requirement | Current permit functions and `ClaimPartiesRecorded` event exist; legacy deployments fail closed for writes |
| Connect | RPC responds, chain ID is Sepolia (`11155111`), target addresses contain bytecode and the live trusted forwarder matches the artifact |

## Public interface

| API | Purpose |
| --- | --- |
| `load_claims_deployment(settings)` | Read and validate the explicitly selected local Ignition artifact |
| `ClaimsDeployment.require_gasless()` | Reject a deployment without the forwarder boundary |
| `ClaimsDeployment.require_public_intake()` | Reject a pre-permit deployment for current writes |
| `connect_claims_deployment(w3, deployment)` | Validate the live network/registry and return a Web3 contract |
| `connect_claims_forwarder(w3, deployment)` | Validate the live forwarder and return its Web3 contract |

## Configure

```dotenv
# Directory name under apps/contracts/ignition/deployments/.
CLAIMS_DEPLOYMENT_ID="sepolia-public-intake-v1"

# RPC must report Sepolia chain ID 11155111.
SEPOLIA_RPC_URL="https://your-reviewed-sepolia-endpoint"
```

The deployment ID is the only selector. Do not add independent
registry-address environment variables to individual services; that would
reintroduce configuration drift.

## Verify

```bash
apps/backend/.venv/bin/python -m pytest \
  packages/integrations/ethereum/tests -q
```

See the [contract deployment guide](../../../apps/contracts/README.md) and the
[root deployment table](../../../README.md#sepolia-deployments).
