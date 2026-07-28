"""Bridge confirmed Sepolia claim events into the off-chain scoring workflow.

For each ``ClaimSubmitted`` event, the listener downloads the referenced IPFS
document and checks its Keccak-256 hash against the value stored by the contract.
Only verified events are published to Kafka. ``ClaimAssessed`` events are also
printed so an operator can follow the claim lifecycle from one terminal.

The listener reads small confirmed block ranges and saves a durable checkpoint.
That makes public RPC failures and normal restarts recoverable without relying
on an in-memory event filter. Configuration and run instructions live in
``listener/README.md`` and the root project guide.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Protocol

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
from observability import ListenerMetrics, ShutdownSignal

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


class ClaimPayloadReader(Protocol):
    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes: ...


class BlockRangeProcessor(Protocol):
    def process_range(self, from_block: int, to_block: int) -> None: ...


class BlockCheckpoint(Protocol):
    def save(self, block_number: int) -> None: ...


class ClaimEventProcessor:
    """Verify confirmed claim logs and publish deterministic scoring events."""

    event_names = ("ClaimSubmitted", "ClaimAssessed")

    def __init__(
        self,
        *,
        chain_id: int,
        contract_address: str,
        contract: Any,
        ipfs: ClaimPayloadReader,
        publisher: ClaimEventPublisher | None,
        metrics: ListenerMetrics | None = None,
    ) -> None:
        """Keep the external adapters needed to process confirmed logs.

        Metrics are optional so the same processor stays lightweight in local
        scripts and tests. The cloud entry point supplies them explicitly.
        """

        self.chain_id = chain_id
        self.contract_address = contract_address
        self.contract = contract
        self.ipfs = ipfs
        self.publisher = publisher
        self.metrics = metrics

    def process_range(self, from_block: int, to_block: int) -> None:
        """Handle all watched logs in canonical blockchain order."""

        entries = []
        for name in self.event_names:
            event_type = getattr(self.contract.events, name)()
            entries.extend(
                event_type.get_logs(
                    from_block=from_block,
                    to_block=to_block,
                )
            )
        # Separate event queries can return interleaved results. Restore chain
        # order so a submission is never observed after its later assessment.
        entries.sort(key=lambda event: (event["blockNumber"], event["logIndex"]))
        for event in entries:
            if event["event"] == "ClaimSubmitted":
                self._handle_claim_submitted(event)
            elif event["event"] == "ClaimAssessed":
                self._handle_claim_assessed(event)
            else:
                raise ValueError(f"Unsupported claim event: {event['event']}")

    def _verified_payload(
        self,
        *,
        claim_id: int,
        pointer: str,
        expected_hash: Any,
    ) -> bytes:
        try:
            payload = self.ipfs.download_pointer(pointer)
        except IPFSError as exc:
            print(f"[IPFSError] claimId={claim_id} pointer={pointer} error={exc}")
            raise RuntimeError(
                f"IPFS verification failed for claim {claim_id}"
            ) from exc

        # Hash the exact downloaded bytes. A gateway response that differs by
        # even one byte must never be published for scoring.
        actual_hash = Web3.keccak(payload)
        if actual_hash != expected_hash:
            print(
                f"[IPFSVerificationFailed] claimId={claim_id} "
                f"expected={hx(expected_hash)} actual={hx(actual_hash)}"
            )
            raise RuntimeError(f"IPFS verification failed for claim {claim_id}")

        print(
            f"[IPFSVerified] claimId={claim_id} pointer={pointer} "
            f"bytes={len(payload)} hash={hx(actual_hash)}"
        )
        return payload

    def _handle_claim_submitted(self, event: Any) -> None:
        # The log carries both the pointer and expected hash, so verification
        # does not trust a browser receipt or a separate backend response.
        args = event["args"]
        print(
            f"[ClaimSubmitted] claimId={args['claimId']} "
            f"claimant={args['claimant']} claimHash={hx(args['claimHash'])} "
            f"dataPointer={args['dataPointer']} block={event['blockNumber']} "
            f"tx={hx(event['transactionHash'])}"
        )
        self._verified_payload(
            claim_id=args["claimId"],
            pointer=args["dataPointer"],
            expected_hash=args["claimHash"],
        )

        if self.publisher is None:
            if self.metrics is not None:
                self.metrics.observe_event("claim_submitted")
            return

        # The event ID is derived from the immutable chain log. Replaying the
        # same confirmed range therefore republishes the same idempotency key.
        claim_event = ClaimSubmittedEvent.create(
            chain_id=self.chain_id,
            contract_address=self.contract_address,
            claim_id=args["claimId"],
            claimant=args["claimant"],
            claim_hash=hx(args["claimHash"]),
            data_pointer=args["dataPointer"],
            block_number=event["blockNumber"],
            block_hash=hx(event["blockHash"]),
            transaction_hash=hx(event["transactionHash"]),
            log_index=event["logIndex"],
            event_timestamp=args["timestamp"],
        )
        self.publisher.publish(claim_event)
        # Count this event only after the producer has received Kafka's
        # acknowledgement. A failed attempt remains visible through the poll
        # error metric and will be retried from the durable block checkpoint.
        if self.metrics is not None:
            self.metrics.observe_event("claim_submitted")
            self.metrics.observe_kafka_publication()
        print(
            f"[KafkaPublished] eventId={claim_event.event_id} "
            f"topic={self.publisher.topic}"
        )

    def _handle_claim_assessed(self, event: Any) -> None:
        args = event["args"]
        raw_status = args["newStatus"]
        status = (
            STATUS_NAMES[raw_status]
            if raw_status < len(STATUS_NAMES)
            else f"?{raw_status}"
        )
        print(
            f"[ClaimAssessed] claimId={args['claimId']} status={status} "
            f"fraudScore={args['fraudScore']} "
            f"({args['fraudScore'] / 100:.2f}%) assessor={args['assessor']} "
            f"block={event['blockNumber']} tx={hx(event['transactionHash'])}"
        )
        if self.metrics is not None:
            self.metrics.observe_event("claim_assessed")


class ConfirmedBlockPoller:
    """Advance a durable checkpoint only after every safe log succeeds."""

    def __init__(
        self,
        *,
        processor: BlockRangeProcessor,
        checkpoint: BlockCheckpoint,
        confirmation_blocks: int,
    ) -> None:
        if confirmation_blocks < 0:
            raise ValueError("confirmation_blocks cannot be negative")
        self.processor = processor
        self.checkpoint = checkpoint
        self.confirmation_blocks = confirmation_blocks

    def process_latest(self, *, latest_block: int, last_processed: int) -> int:
        safe_block = latest_block - self.confirmation_blocks
        if safe_block <= last_processed:
            return last_processed

        self.processor.process_range(last_processed + 1, safe_block)
        # A processing exception exits before this save, guaranteeing that the
        # failed range is retried from the previous durable checkpoint.
        self.checkpoint.save(safe_block)
        return safe_block


def main():
    # Metrics contain no claimant data. The Ops Agent reads this private
    # endpoint on the VM and forwards the samples to Cloud Monitoring.
    metrics = ListenerMetrics.start_from_env()
    shutdown = ShutdownSignal()
    shutdown.install()

    contract_address, abi = load_deployment(IGNITION_DIR, MODULE_ID)
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    # If Sepolia reports an extraData validation error, inject
    # ExtraDataToPOAMiddleware here before the connection check.
    if not w3.is_connected():
        raise SystemExit(f"Could not connect to the RPC endpoint: {RPC_URL}")
    contract = w3.eth.contract(address=contract_address, abi=abi)
    kafka_settings = KafkaSettings.from_env()
    claim_event_publisher = create_publisher(kafka_settings)
    chain_id = w3.eth.chain_id
    state_path = Path(
        os.environ.get(
            "LISTENER_STATE_FILE",
            Path(__file__).with_name(".state")
            / f"claims-{chain_id}-{contract_address.lower()}.json",
        )
    )
    cursor = BlockCursor(state_path, chain_id, contract_address)
    processor = ClaimEventProcessor(
        chain_id=chain_id,
        contract_address=contract_address,
        contract=contract,
        ipfs=IPFSClient.from_env(),
        publisher=claim_event_publisher,
        metrics=metrics,
    )
    poller = ConfirmedBlockPoller(
        processor=processor,
        checkpoint=cursor,
        confirmation_blocks=CONFIRMATION_BLOCKS,
    )

    print(
        f"Listening for {', '.join(processor.event_names)} "
        f"on {contract_address} via {RPC_URL}"
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
        while not shutdown.is_set():
            try:
                latest = w3.eth.block_number
                last_processed = poller.process_latest(
                    latest_block=latest,
                    last_processed=last_processed,
                )
                metrics.observe_poll(
                    latest_block=latest,
                    last_processed_block=last_processed,
                    confirmation_blocks=CONFIRMATION_BLOCKS,
                )
            except Exception as exc:
                # RPC, IPFS and Kafka failures all retry from the saved checkpoint.
                metrics.observe_poll_error()
                print(f"Polling error (will retry): {exc}")
            # Unlike time.sleep, this returns immediately when Docker asks the
            # container to stop, making normal deployments quick and predictable.
            shutdown.wait(POLL_INTERVAL)
    finally:
        if claim_event_publisher is not None:
            claim_event_publisher.close()


if __name__ == "__main__":
    main()
