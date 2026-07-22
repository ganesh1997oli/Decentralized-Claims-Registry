# Claims registry smart contract

This directory contains the Solidity contract that anchors insurance claims on
Ethereum. It stores a hash and an off-chain data pointer rather than the full
claim document, then allows an authorized assessor to record a status and fraud
score.

The contract is intentionally small enough to support the dissertation
prototype. Its owner-and-assessor authorization is custom code and should be
replaced with audited role-based access control before production use.

## Contract behaviour

`ClaimsRegistry.sol` provides the following operations:

- `submitClaim` records the document hash and `ipfs://` pointer.
- `getClaim` returns the current state of one claim.
- `verifyClaimData` hashes supplied bytes and compares them with the stored hash.
- `assessClaim` records a status and a fraud score from `0` to `10,000`.
- `setAssessor` grants or removes assessment permission.
- `transferOwnership` changes the administrative account.

It emits `ClaimSubmitted`, `ClaimAssessed`, `AssessorUpdated`, and
`OwnershipTransferred` events for off-chain consumers.

## Install dependencies

```bash
cd contract
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
cd contract
npx hardhat node
```

Deploy from a second terminal:

```bash
cd contract
npx hardhat ignition deploy \
  ignition/modules/Claimsregistry.ts \
  --network localhost
```

The local deployment is written to
`ignition/deployments/chain-31337/`.

## Deploy to Sepolia

The network configuration expects two Hardhat configuration variables:

- `SEPOLIA_RPC_URL`: an Ethereum Sepolia RPC endpoint;
- `SEPOLIA_PRIVATE_KEY`: a funded Sepolia-only deployment key.

Store them with Hardhat Keystore:

```bash
npx hardhat keystore set SEPOLIA_RPC_URL
npx hardhat keystore set SEPOLIA_PRIVATE_KEY
```

Then deploy:

```bash
npx hardhat ignition deploy \
  ignition/modules/Claimsregistry.ts \
  --network sepolia
```

Ignition resumes an existing deployment for the same chain. Review the displayed
network and address before confirming the transaction.

## Current deployment

- Sepolia chain ID: `11155111`
- Module: `ClaimsRegistryModule#ClaimsRegistry`
- Address: `0x57E3203b9427BE41c753bEedD526D81a66bFc2AB`
- Explorer: [Sepolia Etherscan](https://sepolia.etherscan.io/address/0x57E3203b9427BE41c753bEedD526D81a66bFc2AB)

The deployment module authorizes the deployer as an assessor so the demonstration
can submit and assess a claim immediately. A real deployment should grant that
role only to a deliberately chosen managed signer.

## Important limitations

- Only the hash protects integrity; the `dataPointer` is publicly visible.
- `Approved` and `Rejected` are final states in the current contract.
- The custom authorization has not been externally audited.
- Deployments cost test ETH on Sepolia.
- Never use a private key that controls real funds.
