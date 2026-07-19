"""
Listener for ClaimRegistry events - watches BOTH halves of the loop:

ClaimSubmitted (claimant -> chain) a new claim was anchored on-chain
ClaimAssessed (assessor -> chain) the fraud verdict was written back

Approach: pool `get_logs` over explicit block ranges, tracking the last block processed.
More reliable on hosted RPC providers than eth_newFilter-based subscriptions, and it resumes cleanly after restarts.

Targets web3.py v7.x. On v6.x, change `from_block` / `to_block` to `fromBlock` / `toBlock` in the get_logs call.

Configuration (env vars):
RPC_URL JSON_RPC endpoint (also honors SEPOLIA_RPC_URL)
IGNITION_DIR Hardhat Ignition deployment directory (default: ./contract/ignition)
POLL_INTERVAL seconds between polls (default 5)
CONFIRMATION_BLOCKS reorg-safety confirmations (default 2)

Run AFTER deploying, from the contract/directory:
    npx hardhat ignition deploy ignition/modules/ClaimsRegistry.tx --network sepolia
    python claims_listener.py
"""

import json
import os
import time
from pathlib import Path

from web3 import Web3
# If you hit an "extraData" validation error on Sepolia, uncomment these:
# from web3.middleware import ExtraDataToPOAMiddleware

RPC_URL = (
    os.environ.get("RPC_URL")
    or os.environ.get("SEPOLIA_RPC_URL")
    or "https://ethereum-sepolia-rpc.publicnode.com"
)

DEFAULT_IGNITION_DIR = (
    Path(__file__).resolve().parents[1]
    / "contract"
    / "ignition"
    / "deployments"
    / "chain-11155111"
)

IGNITION_DIR = Path(os.environ.get("IGNITION_DIR", DEFAULT_IGNITION_DIR))

MODULE_ID = os.environ.get("MODULE_ID", "ClaimsRegistryModule#ClaimsRegistry")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
CONFIRMATION_BLOCKS = int(os.environ.get("CONFIRMATION_BLOCKS", "2"))

# Mirrors the contract's `enum Status` declaration order.
STATUS_NAMES = ["Submitted", "UnderReview", "Approved", "Rejected", "Flagged"]


def load_deployment(ignition_dir: Path, module_id: str):
    """Read the deployed address and ABI produced by Hardhat Ignition."""
    addresses = json.loads((ignition_dir / "deployed_addresses.json").read_text())
    artifact = json.loads((ignition_dir / "artifacts" / f"{module_id}.json").read_text())
    return Web3.to_checksum_address(addresses[module_id]), artifact["abi"]

def hx(b) -> str:
    """Hex string with a single 0x prefix, whatever .hex() returns."""
    s = b.hex()
    return s if s.startswith("0x") else f"0x{s}"

CONTRACT_ADDRESS, ABI = load_deployment(IGNITION_DIR, MODULE_ID)

w3 = Web3(Web3.HTTPProvider(RPC_URL))
# w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0) # If extraData error

if not w3.is_connected():
    raise SystemExit(f"Could not connect to the RPC endpoint: {RPC_URL}")

contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=ABI)

def on_claim_submitted(e):
    a = e["args"]
    print(
        f"[ClaimSubmitted] claimId={a['claimId']} claimant={a['claimant']}"
        f"claimHash={hx(a['claimHash'])} dataPointer={a['dataPointer']} "
        f"block={e['blockNumber']} tx={hx(e['transactionHash'])}"
    )

    # Pipeline hook
    # 1. Fetch the payload from a['dataPointer].
    # 2. Verify keccak(payload) == a['claimHash] (or call verifyClaimData).
    # 3. Score it with the fraud model.
    # 4. Write the verdict back with assessClaim (see submit_and_assess_demp.py).

def on_claim_assessed(e):
    a = e["args"]
    raw = a["newStatus"]
    status = STATUS_NAMES[raw] if raw < len(STATUS_NAMES) else f"?{raw}"

    print(
        f"[ClaimAssessed]  claimId={a['claimId']} status={status} "
        f"fraudScore={a['fraudScore']} ({a['fraudScore'] / 100:.2f}%) "
        f"assessor={a['assessor']} "
        f"block={e['blockNumber']} tx={hx(e['transactionHash'])}"
    )
    # Pipeline hook: mark the claim as resolved in your off-chain store,
    # notify downstream systems, close the loop.

HANDLERS = {
    "ClaimSubmitted": on_claim_submitted,
    "ClaimAssessed": on_claim_assessed,
}

def poll_range(from_block: int, to_block: int) -> None:
    """Fetch logs for every watched event and dispatch them in chain order."""
    entries = []
    for name in HANDLERS:
        ev = getattr(contract.events, name)()
        entries.extend(ev.get_logs(from_block=from_block, to_block=to_block))
    entries.sort(key=lambda e: (e["blockNumber"], e["logIndex"]))
    for e in entries:
        HANDLERS[e["event"]](e)

def main():
    print(
        f"Listening for {', '.join(HANDLERS)} on {CONTRACT_ADDRESS} via {RPC_URL}"
    )
    last_processed = w3.eth.block_number
    while True:
        try:
            latest = w3.eth.block_number
            safe_block = latest - CONFIRMATION_BLOCKS
            if safe_block > last_processed:
                poll_range(last_processed + 1, safe_block)
                last_processed = safe_block
        except Exception as exc:
            print(f"Polling error (will retry): {exc}")
        time.sleep(POLL_INTERVAL)
 
 
if __name__ == "__main__":
    main()
