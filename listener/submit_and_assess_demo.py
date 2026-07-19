"""
Drive the full claim loop from Python -- the oracle-side counterpart to
claims_listener.py. Play both roles for demonstration:

1. claimant: submitClaim(claimhash, dataPointer)
2. assessor: assessClaim(claimId, status, fraudScore) <- the write-back

igns raw transaction with PRIVATE_KEY, so the same code works on a local Hardhat
node and on Sepolia. On Sepolia the key must hold ETH for gas, and for step 2 it must have been
authorized via setAssessor (the Ignition module authorizes the deployer, so the deploying key works out of the box).

Env vars:
PRIVATE_KEY required. NEVER commit this or hardcode it.
RPC_URL default default http://127.0.0.1:8545 (local node)
IGNITION_DIR default ignition/deployments/chain-11155111 (Sepolia);
                     use ignition/deployments/chain-31337 for a local node
Targets web3.py v7.x (v6 note: `igned.raw_transaction` was `rawTransaction`).
"""

import json
import os
from pathlib import Path

from web3 import Web3

RPC_URL = os.environ.get("SEPOLIA_RPC_URL", "http://127.0.0.1:8545")

# Resolved relative to THIS FILE, so the script runs from any working directory.
# chain-11155111 = Sepolia. For a local Hardhat node, point at chain-31337
# (or just set the IGNITION_DIR env var).
DEFAULT_IGNITION_DIR = (
    Path(__file__).resolve().parents[1]
    / "contract"
    / "ignition"
    / "deployments"
    / "chain-11155111"
)

IGNITION_DIR = Path(os.environ.get("IGNITION_DIR", DEFAULT_IGNITION_DIR))
MODULE_ID = os.environ.get("MODULE_ID", "ClaimsRegistryModule#ClaimsRegistry")

STATUS_NAMES = ["Submitted", "UnderReview", "Approved", "Rejected", "Flagged"]
FLAGGED = 4 # Status.Flagged

def load_deployment(ignition_dir: Path, module_id: str):
    address = json.loads((ignition_dir / "deployed_addresses.json").read_text())
    artifact = json.loads(
        (ignition_dir / "artifacts" / f"{module_id}.json").read_text()
    )
    return Web3.to_checksum_address(address[module_id]), artifact["abi"]

w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    raise SystemExit(f"Could not connect to {RPC_URL}")

CONTRACT_ADDRESS, ABI = load_deployment(IGNITION_DIR, MODULE_ID)
contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=ABI)

acct = w3.eth.account.from_key(os.environ["SEPOLIA_PRIVATE_KEY"])
print(f"Using account {acct.address} against {CONTRACT_ADDRESS} via {RPC_URL}")

def send(fn):
    """Build, sign, send a contract call; return the receipt."""
    tx = fn.build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": w3.eth.chain_id,
        }
    )
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt["status"] != 1:
        raise SystemExit(f"Transaction reverted: {tx_hash.hex()}")
    return receipt

# ---1. Submit a claim (claimant role) ---
payload = b"policy-42:incident-2026-07-13:demo-payload"
claim_hash = Web3.keccak(payload) # hash of hte canonical off-chain payload
data_pointer = "ipfs://bafy-demo-cid"

claim_id = contract.functions.claimCount().call() # next id == current count
print(f"Submitting claim #{claim_id} ...")
r1 = send(contract.functions.submitClaim(claim_hash, data_pointer))
print(f" mined in block {r1['blockNumber']}")

# ---2. Integrity check (what your pipeline does after fetching the payload) ---
ok = contract.functions.verifyClaimData(claim_id, payload).call()
print(f"verifyClaimData(#{claim_id}) -> {ok}")
assert ok, "sorted hash does not match payload!"

# ---3. Write verdict back (assessor/oracle role) ---
fraud_score = 8500 # basic points = 85.00%
print(f"Assessing claim #{claim_id} as Flagged with score {fraud_score} ...")
r2 = send(contract.functions.assessClaim(claim_id, FLAGGED, fraud_score))
print(f" mined in block {r2['blockNumber']}")

# ---4. Read back the final on-chain state
claim = contract.functions.getClaim(claim_id).call()
print(
    f"Final state: status={STATUS_NAMES[claim[3]]}"
    f"fraudScore={claim[4]} ({claim[4] / 100.0:.2f}%)"
)

print("Done - if claims_listener.py is running, it saw both events.")