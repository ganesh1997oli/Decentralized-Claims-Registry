"""Listen for ClaimRegistry events and verify IPFS-backed claim payloads.

ClaimSubmitted (claimant -> chain) a new claim was anchored on-chain
ClaimAssessed (assessor -> chain) the fraud verdict was written back

Approach: ask for logs in small block ranges and remember the last block read.
This works reliably with hosted RPC services and resumes cleanly after restarts.

Targets web3.py v7.x. On v6.x, change `from_block` / `to_block` to
`fromBlock` / `toBlock` in the get_logs call.

Configuration (env vars):
RPC_URL JSON_RPC endpoint (also honors SEPOLIA_RPC_URL)
IGNITION_DIR Hardhat Ignition deployment directory (default: ./contract/ignition)
IPFS_GATEWAY HTTP gateway base (default: https://gateway.pinata.cloud/ipfs)
POLL_INTERVAL seconds between polls (default 5)
CONFIRMATION_BLOCKS reorg-safety confirmations (default 2)
KAFKA_ENABLED publish verified ClaimSubmitted events when true (default false)
LISTENER_STATE_FILE durable block checkpoint (default: listener/.state/...)
LISTENER_START_BLOCK first block to read when no checkpoint exists (optional)

Run AFTER deploying, from the contract/directory:
    npx hardhat ignition deploy ignition/modules/ClaimsRegistry.tx --network sepolia
    python claims_listener.py
"""

import json
import os
import sys
import time
from pathlib import Path

from web3 import Web3

if __package__:
    from .block_cursor import BlockCursor
else:
    # A directly executed script sees only this folder. Add the repository root
    # so it can reach the shared integrations without requiring installation.
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    from block_cursor import BlockCursor

from integrations.ipfs import IPFSClient, IPFSError
from integrations.kafka import (
    ClaimEventPublisher,
    ClaimSubmittedEvent,
    KafkaSettings,
    create_publisher,
)

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

# Keep this order the same as the Status enum in the Solidity contract.
STATUS_NAMES = ["Submitted", "UnderReview", "Approved", "Rejected", "Flagged"]


def load_deployment(ignition_dir: Path, module_id: str):
    """Read the deployed address and ABI produced by Hardhat Ignition."""
    addresses = json.loads((ignition_dir / "deployed_addresses.json").read_text())
    artifact_path = ignition_dir / "artifacts" / f"{module_id}.json"
    artifact = json.loads(artifact_path.read_text())
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
ipfs = IPFSClient.from_env()
claim_event_publisher: ClaimEventPublisher | None = None


def verify_ipfs_payload(claim_id: int, pointer: str, expected_hash) -> bool:
    """Download the IPFS bytes and check that they match the on-chain hash."""
    try:
        payload = ipfs.download_pointer(pointer)
    except IPFSError as exc:
        print(f"[IPFSError] claimId={claim_id} pointer={pointer} error={exc}")
        return False

    # Hash the bytes exactly as received. Even a one-character change produces
    # a different hash and fails this check.
    actual_hash = Web3.keccak(payload)
    if actual_hash != expected_hash:
        print(
            f"[IPFSVerificationFailed] claimId={claim_id} "
            f"expected={hx(expected_hash)} actual={hx(actual_hash)}"
        )
        return False

    print(
        f"[IPFSVerified] claimId={claim_id} pointer={pointer} "
        f"bytes={len(payload)} hash={hx(actual_hash)}"
    )
    return True


def on_claim_submitted(e):
    # The event already contains everything needed to find and verify the file.
    a = e["args"]
    print(
        f"[ClaimSubmitted] claimId={a['claimId']} claimant={a['claimant']} "
        f"claimHash={hx(a['claimHash'])} dataPointer={a['dataPointer']} "
        f"block={e['blockNumber']} tx={hx(e['transactionHash'])}"
    )
    verified = verify_ipfs_payload(
        a["claimId"], a["dataPointer"], a["claimHash"]
    )
    if not verified:
        # Do not move the durable block cursor past data we could not verify.
        # The same event will be retried after the gateway recovers.
        raise RuntimeError(f"IPFS verification failed for claim {a['claimId']}")

    if claim_event_publisher is not None:
        event = ClaimSubmittedEvent.create(
            chain_id=w3.eth.chain_id,
            contract_address=CONTRACT_ADDRESS,
            claim_id=a["claimId"],
            claimant=a["claimant"],
            claim_hash=hx(a["claimHash"]),
            data_pointer=a["dataPointer"],
            block_number=e["blockNumber"],
            block_hash=hx(e["blockHash"]),
            transaction_hash=hx(e["transactionHash"]),
            log_index=e["logIndex"],
            event_timestamp=a["timestamp"],
        )
        claim_event_publisher.publish(event)
        print(
            f"[KafkaPublished] eventId={event.event_id} "
            f"topic={claim_event_publisher.topic}"
        )


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
    # A future production worker could update PostgreSQL or notify a reviewer here.


HANDLERS = {
    "ClaimSubmitted": on_claim_submitted,
    "ClaimAssessed": on_claim_assessed,
}


def poll_range(from_block: int, to_block: int) -> None:
    """Read watched events and handle them in the order they happened."""
    entries = []
    for name in HANDLERS:
        ev = getattr(contract.events, name)()
        entries.extend(ev.get_logs(from_block=from_block, to_block=to_block))
    entries.sort(key=lambda e: (e["blockNumber"], e["logIndex"]))
    for e in entries:
        HANDLERS[e["event"]](e)


def main():
    global claim_event_publisher

    kafka_settings = KafkaSettings.from_env()
    claim_event_publisher = create_publisher(kafka_settings)
    chain_id = w3.eth.chain_id
    state_path = Path(
        os.environ.get(
            "LISTENER_STATE_FILE",
            Path(__file__).with_name(".state")
            / f"claims-{chain_id}-{CONTRACT_ADDRESS.lower()}.json",
        )
    )
    cursor = BlockCursor(state_path, chain_id, CONTRACT_ADDRESS)

    print(
        f"Listening for {', '.join(HANDLERS)} on {CONTRACT_ADDRESS} via {RPC_URL}"
    )
    if claim_event_publisher is not None:
        print(
            f"Kafka publishing enabled: {kafka_settings.topic} via "
            f"{kafka_settings.bootstrap_servers}"
        )

    first_safe_block = max(0, w3.eth.block_number - CONFIRMATION_BLOCKS)
    start_block = os.environ.get("LISTENER_START_BLOCK")
    first_run_default = int(start_block) - 1 if start_block else first_safe_block
    last_processed = cursor.load(default=first_run_default)
    print(f"Listener checkpoint: {state_path} (last block {last_processed})")

    try:
        while True:
            try:
                latest = w3.eth.block_number
                # Wait for a few newer blocks before processing an event. This lowers
                # the chance of acting on a block that Sepolia later replaces.
                safe_block = latest - CONFIRMATION_BLOCKS
                if safe_block > last_processed:
                    poll_range(last_processed + 1, safe_block)
                    # Save only after every event in the range reached Kafka.
                    cursor.save(safe_block)
                    last_processed = safe_block
            except Exception as exc:
                # RPC, IPFS and Kafka failures all retry from the saved checkpoint.
                print(f"Polling error (will retry): {exc}")
            time.sleep(POLL_INTERVAL)
    finally:
        if claim_event_publisher is not None:
            claim_event_publisher.close()


if __name__ == "__main__":
    main()
