# Solidity security review

Date: 29 July 2026  
Branch: `security/solidity-contract-audit`

This is an internal engineering review of the dissertation prototype, not a
professional third-party audit or a guarantee of security.

> Gasless ERC-2771 changes were implemented later and are outside this dated
> review. They require a new independent review and deployment; see
> `apps/relayer/PRODUCTION.md`.

## Scope

- `contracts/ClaimsRegistry.sol`
- Hardhat Ignition deployment and role parameters
- Backend and worker signing-key boundaries
- Listener handling of immutable invalid claim events

## Implemented remediations

| Finding | Remediation |
| --- | --- |
| Permissionless submissions could feed invalid pointers into the listener | `submitClaim` now requires `SUBMITTER_ROLE`, accepts only a bounded bare `ipfs://CID`, and the listener quarantines permanent invalid events |
| Administration, submission and assessment reused one wallet | The contract enforces distinct admin, submitter and assessor addresses; application processes load separate environment keys |
| Any assessor could modify any insurer's claim | Every assessor is explicitly scoped to authorized insurer wallets |
| Status and score could be rewritten arbitrarily | The lifecycle is forward-only and the initial model score cannot change during later transitions |
| Ownership transfer was immediate and one-step | OpenZeppelin `AccessControlDefaultAdminRules` adds a configurable delay and explicit acceptance |
| Generic role calls could bypass scoped-role setup | Submitter and assessor roles can only be changed through their invariant-preserving configuration functions |

## Verification

- Solidity and TypeScript contract tests: 30 passed.
- Python backend, listener, IPFS and Kafka tests: 63 passed, 3 integration
  tests skipped because their external services were not enabled.
- Docker Compose configuration validation: passed.
- TypeScript type-check: passed.
- `npm audit --omit=dev`: no production dependency vulnerabilities reported.

The contract tests cover unauthorized submission, malformed pointers,
cross-insurer assessment, invalid status regression, score replacement,
role-reuse attempts, delayed administration transfer, and fraud-score fuzzing.

## Residual risks and limitations

- IPFS payloads and pointers remain public and unencrypted. Only fictional,
  synthetic research data is permitted.
- A compromised default administrator can immediately change submitter and
  assessor roles. A production design should use a multisignature account or
  timelock and managed signing infrastructure.
- RPC, IPFS, Kafka and Sepolia remain availability dependencies.
- The contract is not upgradeable. Remediation requires deploying a new
  contract and intentionally migrating application configuration.
- The development toolchain still reports advisories in development-only npm
  packages. These packages are not shipped in the application runtime, but
  should be reviewed during routine dependency upgrades.

## Deployment status

After explicit project-owner authorization, the hardened contract was deployed
to Sepolia as Ignition deployment `sepolia-security-audit-v1` at
`0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB`. Read-only verification confirmed
the deployed bytecode, distinct role accounts, assessor-to-submitter scope,
86,400-second administration-transfer delay, and zero initial claims.

The earlier checked-in address remains the legacy pre-remediation contract and
must not be used to validate these security controls.
