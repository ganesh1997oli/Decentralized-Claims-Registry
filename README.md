# Decentralized Claims Registry

On-chain insurance-claims registry (Solidity / Hardhat 3) with an off-chain
Python listener that feeds claim events to an AI fraud-detection pipeline and
writes verdicts back on-chain.

```
contract/   Solidity contract, Ignition deploy modules, tests (TS + Solidity)
listener/   Python: event listener + submit/assess demo (the oracle side)
```

## Prerequisites

- Node.js 22+ and npm
- Python 3.10+
- git

## 1. Install and test the contract

```bash
cd contract
npm install
npx hardhat test          # runs Solidity tests + TypeScript tests
```

All tests must pass before anything else. (`npx hardhat compile` also fixes
editor errors like "'claim' is of type 'unknown'" after a fresh clone.)

## 2. Set up the Python listener

```bash
cd listener
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Full local end-to-end run (three terminals)

Terminal A - local chain:
```bash
cd contract && npx hardhat node
```

Terminal B - deploy, then start the listener:
```bash
cd contract
npx hardhat ignition deploy ignition/modules/Claimsregistry.ts --network localhost

cd ../listener && source .venv/bin/activate
RPC_URL=http://127.0.0.1:8545 \
IGNITION_DIR=../contract/ignition/deployments/chain-31337 \
POLL_INTERVAL=1 CONFIRMATION_BLOCKS=0 \
python claims_listener.py
```

Terminal C - submit a claim and write a fraud verdict back:
```bash
cd listener && source .venv/bin/activate
RPC_URL=http://127.0.0.1:8545 \
IGNITION_DIR=../contract/ignition/deployments/chain-31337 \
PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 \
python submit_and_assess_demo.py
```

That PRIVATE_KEY is Hardhat's publicly known dev account #0 - safe on a local
node, never usable on a real network. Terminal B should print a
`[ClaimSubmitted]` line followed by `[ClaimAssessed]`.

## 4. Sepolia

```bash
cd contract
cp .env.example .env      # fill in a FRESH funded key, then:
set -a; source .env; set +a
npx hardhat ignition deploy ignition/modules/Claimsregistry.ts --network sepolia

cd ../listener && source .venv/bin/activate
python claims_listener.py                    # defaults target Sepolia
```

The listener reads the deployed address and ABI straight from
`contract/ignition/deployments/chain-11155111/`, so there is nothing to copy
by hand after deployment. Commit that directory when it changes - it is what
lets a fresh clone find the contract.