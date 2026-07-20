"""Drive the full IPFS-backed claim loop from Python.

Play both roles for demonstration:

1. claimant: upload a synthetic claim to IPFS, then submitClaim(hash, pointer)
2. assessor: assessClaim(claimId, status, fraudScore) <- the write-back

The script signs raw transactions with SEPOLIA_PRIVATE_KEY. On Sepolia the key
must hold test ETH and must be authorized as an assessor. The Ignition module
authorizes the deployer, so the deploying key works for this demo.

Env vars:
SEPOLIA_PRIVATE_KEY required. NEVER commit or hardcode it.
PINATA_JWT required. Pinata token with public Files write access.
SEPOLIA_RPC_URL defaults to http://127.0.0.1:8545.
IPFS_GATEWAY defaults to https://gateway.pinata.cloud/ipfs.
IGNITION_DIR defaults to the Sepolia deployment directory.
"""

import argparse
import json
import os
import re
from pathlib import Path

from web3 import Web3
from web3.exceptions import Web3RPCError

from ipfs_client import IPFSClient, IPFSError


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
FLAGGED = 4  # Status.Flagged

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--assess-existing",
    type=int,
    metavar="CLAIM_ID",
    help="skip IPFS upload/submission and assess an existing Submitted claim",
)
args = parser.parse_args()


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

private_key = os.environ.get("SEPOLIA_PRIVATE_KEY")
if not private_key:
    raise SystemExit("SEPOLIA_PRIVATE_KEY is required to sign the demo transactions")

acct = w3.eth.account.from_key(private_key)
print(f"Using account {acct.address} against {CONTRACT_ADDRESS} via {RPC_URL}")

next_nonce: int | None = None


def send(fn):
    """Build, sign, send a contract call; return the receipt."""
    global next_nonce

    # Hosted RPC endpoints can briefly return a stale transaction count after
    # a transaction is mined. Read the pending count once, then allocate
    # sequential nonces locally for the rest of this process.
    if next_nonce is None:
        next_nonce = w3.eth.get_transaction_count(acct.address, "pending")

    for attempt in range(2):
        nonce = next_nonce
        tx = fn.build_transaction(
            {
                "from": acct.address,
                "nonce": nonce,
                "chainId": w3.eth.chain_id,
            }
        )
        signed = acct.sign_transaction(tx)
        try:
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        except Web3RPCError as exc:
            match = re.search(r"next nonce\s+(\d+)", str(exc), re.IGNORECASE)
            if attempt == 0 and match and int(match.group(1)) > nonce:
                next_nonce = int(match.group(1))
                print(
                    f"RPC nonce was stale ({nonce}); retrying with "
                    f"nonce {next_nonce} ..."
                )
                continue
            raise
        next_nonce = nonce + 1
        break

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt["status"] != 1:
        raise SystemExit(f"Transaction reverted: {tx_hash.hex()}")
    return receipt

# --- 1. Upload and submit a new claim, or resume one whose assessment failed ---
if args.assess_existing is None:
    claim_id = contract.functions.claimCount().call()  # next id == current count
    claim_document = {
        "schemaVersion": 1,
        "claimReference": f"synthetic-claim-{claim_id}",
        "policyReference": "synthetic-policy-42",
        "claimType": "vehicle_damage",
        "incidentDate": "2026-07-13",
        "amountPence": 250000,
        "description": "Synthetic bumper damage claim for IPFS integration testing",
        "evidence": [],
    }
    payload = json.dumps(
        claim_document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    try:
        ipfs = IPFSClient.from_env(require_upload=True)
        cid = ipfs.upload_bytes(
            payload,
            filename=f"claim-{claim_id}.json",
            content_type="application/json",
        )
        data_pointer = f"ipfs://{cid}"
        downloaded_payload = ipfs.download_pointer(data_pointer)
    except IPFSError as exc:
        raise SystemExit(f"IPFS setup failed: {exc}") from exc

    if downloaded_payload != payload:
        raise SystemExit(
            "IPFS round-trip verification failed before blockchain submission"
        )

    claim_hash = Web3.keccak(payload)
    print(f"Uploaded synthetic claim to {data_pointer}")
    print(f"Gateway URL: {ipfs.gateway_url(data_pointer)}")
    print(f"IPFS round-trip: PASSED ({len(payload)} bytes)")

    print(f"Submitting claim #{claim_id} ...")
    r1 = send(contract.functions.submitClaim(claim_hash, data_pointer))
    print(f" mined in block {r1['blockNumber']}")

    # Confirm the exact IPFS bytes match the on-chain commitment.
    ok = contract.functions.verifyClaimData(claim_id, payload).call()
    print(f"verifyClaimData(#{claim_id}) -> {ok}")
    assert ok, "IPFS payload hash does not match the on-chain claim hash"
else:
    claim_id = args.assess_existing
    if claim_id < 0:
        raise SystemExit("CLAIM_ID must be zero or greater")
    existing_claim = contract.functions.getClaim(claim_id).call()
    current_status = existing_claim[3]
    if current_status != 0:
        raise SystemExit(
            f"Claim #{claim_id} is already {STATUS_NAMES[current_status]}; "
            "refusing to assess it twice"
        )
    print(
        f"Resuming claim #{claim_id} at {existing_claim[2]} "
        "without uploading or submitting another claim"
    )

# --- 2. Write a fixed demo verdict back (assessor/oracle role) ---
fraud_score = 8500  # basis points = 85.00%
print(f"Assessing claim #{claim_id} as Flagged with score {fraud_score} ...")
r2 = send(contract.functions.assessClaim(claim_id, FLAGGED, fraud_score))
print(f" mined in block {r2['blockNumber']}")

# --- 3. Read back the final on-chain state ---
claim = contract.functions.getClaim(claim_id).call()
print(
    f"Final state: status={STATUS_NAMES[claim[3]]} "
    f"fraudScore={claim[4]} ({claim[4] / 100.0:.2f}%) "
    f"dataPointer={claim[2]}"
)

print("Done - if claims_listener.py is running, it saw both events.")
