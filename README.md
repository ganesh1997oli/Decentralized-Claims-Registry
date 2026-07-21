# Decentralized Claims Registry

On-chain insurance-claims registry (Solidity / Hardhat 3) with an off-chain
Python listener that feeds claim events to an AI fraud-detection pipeline and
writes verdicts back on-chain.

```
contract/   Solidity contract, Ignition deploy modules, tests (TS + Solidity)
listener/   Python: event listener + submit/assess demo (the oracle side)
backend/    FastAPI: validate, upload and submit synthetic claims (Week 3)
```

## Prerequisites

- Node.js 22+ and npm
- Python 3.10+
- git
- A Pinata account and JWT with public Files write access

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
cp .env.example .env.local
# Fill in PINATA_JWT and, for Sepolia, the RPC URL and test-wallet key.
# Then load it into each shell that runs a Python script:
set -a; source .env.local; set +a
```

Security note: this repository previously tracked `listener/.env`. Never add a
Pinata JWT to that legacy file. Treat any private key already committed there as
compromised, replace it with a fresh Sepolia-only key, and remove the tracked
file from Git before publishing the repository.

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
SEPOLIA_RPC_URL=http://127.0.0.1:8545 \
IGNITION_DIR=../contract/ignition/deployments/chain-31337 \
POLL_INTERVAL=1 CONFIRMATION_BLOCKS=0 \
python claims_listener.py
```

Terminal C - submit a claim and write a fraud verdict back:
```bash
cd listener && source .venv/bin/activate
set -a; source .env.local; set +a
SEPOLIA_RPC_URL=http://127.0.0.1:8545 \
IGNITION_DIR=../contract/ignition/deployments/chain-31337 \
SEPOLIA_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 \
python submit_and_assess_demo.py
```

That SEPOLIA_PRIVATE_KEY is Hardhat's publicly known dev account #0 - safe on a
local node, never usable on a real network. The submitter uploads a synthetic
JSON claim to public IPFS before it sends the transaction. Terminal B should
print `[ClaimSubmitted]`, `[IPFSVerified]`, and `[ClaimAssessed]`.

## 4. Sepolia

```bash
cd contract
cp .env.example .env      # fill in a FRESH funded key, then:
set -a; source .env; set +a
npx hardhat ignition deploy ignition/modules/Claimsregistry.ts --network sepolia

cd ../listener && source .venv/bin/activate
cp .env.example .env.local  # first run only; fill in test key + Pinata JWT
set -a; source .env.local; set +a
python claims_listener.py                    # terminal 1: defaults to Sepolia

# In terminal 2, activate/load the same listener environment, then:
python submit_and_assess_demo.py
```

If submission succeeds but assessment is interrupted, resume the existing
claim without uploading or submitting a duplicate:

```bash
python submit_and_assess_demo.py --assess-existing 1  # replace 1 with its ID
```

The listener reads the deployed address and ABI straight from
`contract/ignition/deployments/chain-11155111/`, so there is nothing to copy
by hand after deployment. Commit that directory when it changes - it is what
lets a fresh clone find the contract.

## 5. What the IPFS integration proves

`submit_and_assess_demo.py` creates a canonical synthetic claim JSON document,
uploads the exact bytes to public IPFS through Pinata, downloads them once as a
preflight check, and stores both `ipfs://<CID>` and their Keccak-256 hash in the
claim registry. When `claims_listener.py` receives `ClaimSubmitted`, it fetches
the CID through `IPFS_GATEWAY` and independently compares the downloaded bytes
with the on-chain hash.

This milestone deliberately uses synthetic data and does not encrypt it. Never
upload real names, addresses, photographs, policy documents, or other personal
data to public IPFS.

## 6. Week 3 FastAPI backend

The `backend/` service turns the existing IPFS and Sepolia submission demo into
an HTTP API suitable for the proposal's later React form. It exposes
`POST /claims` and returns the assigned claim ID and transaction hash.

See [`backend/README.md`](backend/README.md) for installation, tests, environment
configuration and a complete example request.
