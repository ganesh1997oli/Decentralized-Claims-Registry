# Claims registry smart contract

This directory contains the Solidity contract that anchors insurance claims on
Ethereum. It stores a hash and an off-chain data pointer rather than the full
claim document, then allows an authorized assessor to record a status and fraud
score.

The contract uses OpenZeppelin `AccessControlDefaultAdminRules` for a single
default administrator, delayed two-step administration transfer, and explicit
submitter and assessor roles. This reduces administrative and key-compromise
risk, but does not make the dissertation prototype a production insurance
system or an externally audited contract.

Administration, submission, and assessment must use three different addresses;
the contract rejects role reuse both during deployment and later administration.
The implemented findings and remaining limitations are recorded in
[SECURITY_AUDIT.md](SECURITY_AUDIT.md).

## Contract behaviour

`ClaimsRegistry.sol` provides the following operations:

- `submitClaim` allows a registered insurer service account to record a document
  hash and a bare `ipfs://CID` pointer.
- `getClaim` returns the current state of one claim.
- `verifyClaimData` hashes supplied bytes and compares them with the stored hash.
- `assessClaim` enforces forward-only status changes and a score from `0` to
  `10,000`. The first score is immutable during later finalization.
- `setSubmitter` grants or removes submission permission.
- `setAssessor` scopes an assessor to one registered insurer.
- `beginDefaultAdminTransfer` and `acceptDefaultAdminTransfer` provide delayed,
  two-step administration transfer.

It emits claim lifecycle events plus OpenZeppelin role and admin-transfer events
for off-chain consumers.

Allowed claim transitions are:

```text
Submitted ──► UnderReview ──► Approved | Rejected | Flagged
     └────────► Flagged ─────► Approved | Rejected
```

## Install dependencies

```bash
cd apps/contracts
npm install
```

The project uses Hardhat 3, Solidity `0.8.28`, Viem, TypeScript tests with
`node:test`, and Foundry-compatible Solidity tests.

## Compile and test

```bash
npx hardhat compile
npx hardhat test
```

Run only one test family when working on a specific layer:

```bash
npx hardhat test solidity
npx hardhat test nodejs
```

## Run on a local Hardhat network

Start the node in one terminal:

```bash
cd apps/contracts
npx hardhat node
```

Deploy from a second terminal:

```bash
cd apps/contracts
cp ignition/parameters/sepolia.json.example /tmp/claims-local.json
# Replace the three addresses with distinct local Hardhat accounts.
npx hardhat ignition deploy \
  ignition/modules/Claimsregistry.ts \
  --parameters /tmp/claims-local.json \
  --network localhost
```

The local deployment is written to
`ignition/deployments/chain-31337/`.

## Deploy to Sepolia

The network configuration expects two Hardhat configuration variables:

- `SEPOLIA_RPC_URL`: an Ethereum Sepolia RPC endpoint;
- `SEPOLIA_DEPLOYER_PRIVATE_KEY`: a funded Sepolia-only deployment key that is
  not reused by the backend or scoring worker.

Store them with Hardhat Keystore:

```bash
npx hardhat keystore set SEPOLIA_RPC_URL
npx hardhat keystore set SEPOLIA_DEPLOYER_PRIVATE_KEY
```

Create an ignored parameter file and replace all placeholder addresses. Use
different accounts for administration, submission, and assessment:

```bash
cp ignition/parameters/sepolia.json.example \
  ignition/parameters/sepolia.json
```

Then review and deploy:

```bash
npx hardhat ignition deploy \
  ignition/modules/Claimsregistry.ts \
  --parameters ignition/parameters/sepolia.json \
  --network sepolia
```

Ignition resumes an existing deployment for the same chain. Review the displayed
network and address before confirming the transaction.

## Hardened deployment

- Sepolia chain ID: `11155111`
- Module: `ClaimsRegistryModule#ClaimsRegistry`
- Deployment ID: `sepolia-security-audit-v1`
- Address: `0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB`
- Explorer: [Sepolia Etherscan](https://sepolia.etherscan.io/address/0x2AbAbD3553d5963A4844328B7b42DbC5795B78cB)

The deployed roles were verified on-chain after deployment: the three accounts
are distinct, the assessor is scoped to the submitter, and the administration
transfer delay is 86,400 seconds.

## Legacy deployment

- Sepolia chain ID: `11155111`
- Module: `ClaimsRegistryModule#ClaimsRegistry`
- Address: `0x57E3203b9427BE41c753bEedD526D81a66bFc2AB`
- Explorer: [Sepolia Etherscan](https://sepolia.etherscan.io/address/0x57E3203b9427BE41c753bEedD526D81a66bFc2AB)

That address contains the contract version from before this security hardening.
It does not provide the role scoping, lifecycle checks, pointer constraints, or
two-step administration described above.

## Important limitations

- Only the hash protects integrity; the `dataPointer` is publicly visible.
- Public, unencrypted IPFS is restricted to fictional research data.
- OpenZeppelin supplies the access-control primitives, but the complete custom
  contract has not received an independent professional audit.
- Role wallets still require operational protection and Sepolia test ETH.
- Deployments cost test ETH on Sepolia.
- Never use a private key that controls real funds.
