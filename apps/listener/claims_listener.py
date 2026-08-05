"""Bridge confirmed Sepolia claim events into the off-chain scoring workflow.

For each ``ClaimSubmitted`` event, the listener downloads the referenced IPFS
document and checks its Keccak-256 hash against the value stored by the contract.
Only verified events are published to Kafka. ``ClaimAssessed`` events are also
printed so an operator can follow the claim lifecycle from one terminal.

The listener reads small confirmed block ranges, projects each public event into
PostgreSQL, and saves a deployment-scoped database checkpoint. That makes public
RPC failures, container replacement, and normal restarts recoverable without
relying on an in-memory event filter. Configuration and run instructions live
in ``apps/listener/README.md`` and the root project guide.
"""

import json
import os
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from web3 import Web3

if not __package__:
    # A directly executed script sees only this folder. Add the repository root
    # so it can reach the shared integrations without requiring installation.
    repository_root = str(Path(__file__).resolve().parents[2])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

from packages.integrations.ethereum import (
    DeploymentConfigurationError,
    DeploymentValidationError,
    connect_claims_deployment,
    load_claims_deployment,
)
from packages.integrations.ipfs import InvalidIPFSPointer, IPFSClient, IPFSError
from packages.integrations.kafka import (
    ClaimEventPublisher,
    ClaimSubmittedEvent,
    KafkaSettings,
    create_publisher,
)
from packages.integrations.postgres import (
    PostgresClaimIndexCheckpoint,
    PostgresRepositories,
)
from packages.observability import (
    ListenerMetrics,
    ShutdownSignal,
    configure_logging,
    get_event_logger,
)

logger = get_event_logger(__name__)

# If you hit an "extraData" validation error on Sepolia, uncomment these:
# from web3.middleware import ExtraDataToPOAMiddleware

RPC_URL = (
    os.environ.get("RPC_URL")
    or os.environ.get("SEPOLIA_RPC_URL")
    or "https://ethereum-sepolia.publicnode.com"
)

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
CONFIRMATION_BLOCKS = int(os.environ.get("CONFIRMATION_BLOCKS", "12"))
# Fifty blocks is deliberately conservative for an unauthenticated public RPC.
# Raising this to 250 or 500 can drain a stale checkpoint in fewer requests, but
# it also makes each eth_getLogs call heavier and more likely to hit a provider's
# timeout, result-size, or rate-limit boundary. This is an operator-tuned query
# size, not a confirmation rule or a universal production recommendation.
MAX_BLOCK_RANGE = int(os.environ.get("MAX_BLOCK_RANGE", "50"))

# Keep this order the same as the Status enum in the Solidity contract.
STATUS_NAMES = ["Submitted", "UnderReview", "Approved", "Rejected", "Flagged"]


def hx(b: Any) -> str:
    """Normalize a Web3 byte value to one lower-level ``0x`` hex representation.

    ``HexBytes.hex()`` and ``bytes.hex()`` differ on whether they include the
    prefix. Keeping that compatibility detail here prevents event identifiers,
    database values, and structured logs from representing the same chain value
    in two different forms.
    """
    s = b.hex()
    return s if s.startswith("0x") else f"0x{s}"


def deployment_state_paths(
    settings: Mapping[str, str],
    *,
    deployment_id: str,
    chain_id: int,
    contract_address: str,
) -> tuple[Path, Path]:
    """Derive deployment-scoped paths for local operational artifacts.

    The PostgreSQL checkpoint is authoritative; the checkpoint-shaped path is
    retained for compatibility with older tooling. The dead-letter path must be
    scoped by deployment, chain, and normalized contract address so switching a
    local environment cannot mix quarantined events from two registries.
    Explicit environment values override the generated defaults.
    """

    state_dir = Path(
        settings.get(
            "LISTENER_STATE_DIR",
            str(Path(__file__).with_name(".state")),
        )
    )
    state_name = f"{deployment_id}-{chain_id}-{contract_address.lower()}"
    state_path = Path(
        settings.get(
            "LISTENER_STATE_FILE",
            str(state_dir / f"{state_name}-checkpoint.json"),
        )
    )
    dead_letter_path = Path(
        settings.get(
            "LISTENER_DEAD_LETTER_FILE",
            str(state_dir / f"{state_name}-dead-letter.jsonl"),
        )
    )
    return state_path, dead_letter_path


def required_listener_start_block(settings: Mapping[str, str]) -> int:
    """Require an explicit replay origin before external adapters are created.

    Guessing the current safe head is operationally convenient but unsafe for
    an index: the service would report a healthy checkpoint while every earlier
    claim was absent. Deployment configuration must therefore carry its known
    creation block.
    """

    raw_value = settings.get("LISTENER_START_BLOCK", "").strip()
    if not raw_value:
        raise ValueError(
            "LISTENER_START_BLOCK is required for the PostgreSQL claim index; "
            "set it to the ClaimsRegistry deployment block"
        )
    try:
        block_number = int(raw_value)
    except ValueError as exc:
        raise ValueError("LISTENER_START_BLOCK must be a non-negative integer") from exc
    if block_number < 0:
        raise ValueError("LISTENER_START_BLOCK must be a non-negative integer")
    return block_number


class ClaimPayloadReader(Protocol):
    """Minimal IPFS read capability required by the event processor."""

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes:
        """Return exact content bytes or raise a typed pointer/download error."""

        ...


class BlockRangeProcessor(Protocol):
    """Process every watched log in one inclusive blockchain range."""

    def process_range(self, from_block: int, to_block: int) -> None:
        """Finish all side effects for the range or raise before checkpointing."""

        ...


class BlockCheckpoint(Protocol):
    """Durably record the last block whose entire range completed."""

    def save(self, block_number: int) -> None:
        """Persist the inclusive end block without allowing regression."""

        ...


class ClaimIndexWriter(Protocol):
    """Narrow persistence boundary used while handling confirmed logs."""

    def index_claim_submitted(self, **values: Any) -> None:
        """Idempotently project one confirmed ``ClaimSubmitted`` log."""

        ...

    def index_claim_assessed(self, **values: Any) -> None:
        """Idempotently apply one confirmed ``ClaimAssessed`` log."""

        ...


class PermanentClaimEventError(RuntimeError):
    """An invalid immutable event that cannot become valid on a later retry."""


class DeadLetterSink(Protocol):
    """Persist immutable events that cannot succeed on a later replay."""

    def record(self, event: Any, error: PermanentClaimEventError) -> None:
        """Record enough public provenance to diagnose or explicitly replay."""

        ...


class JsonlDeadLetterSink:
    """Durably record rejected public chain events for operator review."""

    def __init__(self, path: Path) -> None:
        """Target an append-only JSONL file without creating it prematurely.

        The parent directory and file are created only when a permanent failure
        occurs. That keeps a healthy listener from creating unused local state
        files while making each rejected log independently parseable and recoverable.
        """

        self.path = path

    def record(self, event: Any, error: PermanentClaimEventError) -> None:
        """Append the public event identity and rejection reason as one record.

        The raw downloaded document is deliberately excluded: it is not needed
        to replay the blockchain log and may contain private claim information.
        ``transactionHash:logIndex`` lets an operator correlate this entry with
        both Etherscan and the database audit stream.
        """

        args = event["args"]
        transaction_hash = hx(event["transactionHash"])
        entry = {
            "recordedAt": datetime.now(timezone.utc).isoformat(),
            "eventId": f"{transaction_hash}:{event['logIndex']}",
            "event": event["event"],
            "claimId": args["claimId"],
            "blockNumber": event["blockNumber"],
            "transactionHash": transaction_hash,
            "logIndex": event["logIndex"],
            "dataPointer": args["dataPointer"],
            "reason": str(error),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as dead_letter_file:
            dead_letter_file.write(
                json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
            )


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
        indexer: ClaimIndexWriter | None = None,
        metrics: ListenerMetrics | None = None,
        dead_letter: DeadLetterSink | None = None,
    ) -> None:
        """Keep the external adapters needed to process confirmed logs.

        ``contract`` supplies confirmed logs, ``ipfs`` verifies submitted bytes,
        and ``publisher`` forwards verified work to scoring. ``indexer`` is
        optional for isolated tests but the production entry point always
        supplies it. Optional metrics and dead-letter adapters keep the core
        ordering rules testable without starting infrastructure.
        """

        self.chain_id = chain_id
        self.contract_address = contract_address
        self.contract = contract
        self.ipfs = ipfs
        self.publisher = publisher
        self.indexer = indexer
        self.metrics = metrics
        self.dead_letter = dead_letter

    def process_range(self, from_block: int, to_block: int) -> None:
        """Handle every watched log in an inclusive range in canonical order.

        Web3 exposes one event type per query, so results are merged and sorted
        by ``(blockNumber, logIndex)`` before dispatch. A permanent content error
        is quarantined and processing continues; any transient adapter failure
        escapes, preventing the poller from advancing its checkpoint and causing
        the whole idempotent range to be retried.
        """

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
            try:
                if event["event"] == "ClaimSubmitted":
                    self._handle_claim_submitted(event)
                elif event["event"] == "ClaimAssessed":
                    self._handle_claim_assessed(event)
                else:
                    raise ValueError(f"Unsupported claim event: {event['event']}")
            except PermanentClaimEventError as exc:
                # The event is already immutable on-chain. Reprocessing the same
                # malformed pointer or hash can never fix it, so record it
                # durably and allow later claims in the range to make progress.
                if self.dead_letter is None:
                    raise
                self.dead_letter.record(event, exc)
                logger.warning(
                    "claim.quarantined",
                    claim_id=event["args"]["claimId"],
                    block_number=event["blockNumber"],
                    reason=str(exc),
                )

    def _verified_payload(
        self,
        *,
        claim_id: int,
        pointer: str,
        expected_hash: Any,
    ) -> bytes:
        """Download and byte-verify the IPFS document anchored by one event.

        Invalid pointers and hash mismatches are permanent because the confirmed
        log cannot change. Gateway/network failures remain retryable. Hashing the
        exact response bytes—without JSON parsing or normalization—preserves the
        Solidity commitment's byte-for-byte meaning.
        """

        try:
            payload = self.ipfs.download_pointer(pointer)
        except InvalidIPFSPointer as exc:
            logger.warning(
                "ipfs.pointer_invalid",
                claim_id=claim_id,
                error_type=type(exc).__name__,
            )
            raise PermanentClaimEventError(
                f"Invalid IPFS pointer for claim {claim_id}: {exc}"
            ) from exc
        except IPFSError as exc:
            logger.warning(
                "ipfs.download_failed",
                claim_id=claim_id,
                data_pointer=pointer,
                error_type=type(exc).__name__,
            )
            raise RuntimeError(
                f"IPFS verification failed for claim {claim_id}"
            ) from exc

        # Hash the exact downloaded bytes. A gateway response that differs by
        # even one byte must never be published for scoring.
        actual_hash = Web3.keccak(payload)
        if actual_hash != expected_hash:
            logger.error(
                "ipfs.verification_failed",
                claim_id=claim_id,
                expected_hash=hx(expected_hash),
                actual_hash=hx(actual_hash),
            )
            raise PermanentClaimEventError(f"IPFS hash mismatch for claim {claim_id}")

        logger.info(
            "ipfs.verified",
            claim_id=claim_id,
            data_pointer=pointer,
            payload_bytes=len(payload),
            claim_hash=hx(actual_hash),
        )
        return payload

    def _handle_claim_submitted(self, event: Any) -> None:
        """Project, verify, and optionally publish one submission in that order.

        PostgreSQL is written first so the public audit log is captured even if
        IPFS or Kafka is temporarily unavailable. The checkpoint advances only
        after all three stages succeed; on replay, the database upsert and the
        Kafka event ID are deterministic, allowing downstream idempotency.
        """

        # The log carries both the pointer and expected hash, so verification
        # does not trust a browser receipt or a separate backend response.
        args = event["args"]
        logger.info(
            "claim.submitted",
            claim_id=args["claimId"],
            claimant=args["claimant"],
            claim_hash=hx(args["claimHash"]),
            data_pointer=args["dataPointer"],
            block_number=event["blockNumber"],
            transaction_hash=hx(event["transactionHash"]),
        )
        # Index the immutable public log before the slower IPFS/Kafka work. If a
        # later adapter fails, the range is replayed; the database upsert is
        # deliberately idempotent and cannot regress a newer assessment.
        if self.indexer is not None:
            self.indexer.index_claim_submitted(
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
        logger.info(
            "kafka.claim_published",
            event_id=claim_event.event_id,
            claim_id=claim_event.claim_id,
            transaction_hash=claim_event.transaction_hash,
            topic=self.publisher.topic,
        )

    def _handle_claim_assessed(self, event: Any) -> None:
        """Apply the latest assessment state and record its immutable audit log.

        Assessment logs contain no IPFS payload and are not scoring inputs, so
        they update only the PostgreSQL projection and observability stream. The
        repository enforces chain-position ordering during a range replay.
        """

        args = event["args"]
        raw_status = args["newStatus"]
        status = (
            STATUS_NAMES[raw_status]
            if raw_status < len(STATUS_NAMES)
            else f"?{raw_status}"
        )
        if self.indexer is not None:
            self.indexer.index_claim_assessed(
                chain_id=self.chain_id,
                contract_address=self.contract_address,
                claim_id=args["claimId"],
                status=raw_status,
                fraud_score=args["fraudScore"],
                block_number=event["blockNumber"],
                block_hash=hx(event["blockHash"]),
                transaction_hash=hx(event["transactionHash"]),
                log_index=event["logIndex"],
                event_timestamp=args["timestamp"],
            )
        logger.info(
            "claim.assessed",
            claim_id=args["claimId"],
            status=status,
            fraud_score=args["fraudScore"],
            assessor=args["assessor"],
            block_number=event["blockNumber"],
            transaction_hash=hx(event["transactionHash"]),
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
        max_block_range: int | None = None,
    ) -> None:
        """Configure confirmation depth and the maximum inclusive query range.

        ``confirmation_blocks`` defines the safe head; ``max_block_range`` only
        limits provider workload and does not weaken confirmation semantics.
        Checkpoint persistence is injected so advancement can be tested without
        a live PostgreSQL database.
        """

        if confirmation_blocks < 0:
            raise ValueError("confirmation_blocks cannot be negative")
        if max_block_range is not None and max_block_range < 1:
            raise ValueError("max_block_range must be at least 1")
        self.processor = processor
        self.checkpoint = checkpoint
        self.confirmation_blocks = confirmation_blocks
        self.max_block_range = max_block_range

    def process_latest(self, *, latest_block: int, last_processed: int) -> int:
        """Process at most one safe range and return its durable end block.

        Blocks newer than ``latest_block - confirmation_blocks`` are ignored to
        reduce reorganization risk. The checkpoint is saved only after the full
        range succeeds, so an exception leaves ``last_processed`` authoritative
        and makes the next poll replay exactly the unfinished range.
        """

        safe_block = latest_block - self.confirmation_blocks
        if safe_block <= last_processed:
            return last_processed

        range_end = safe_block
        if self.max_block_range is not None:
            range_end = min(
                range_end,
                last_processed + self.max_block_range,
            )

        self.processor.process_range(last_processed + 1, range_end)
        # A processing exception exits before this save, guaranteeing that the
        # failed range is retried from the previous durable checkpoint.
        self.checkpoint.save(range_end)
        return range_end

    def process_to_safe_head(
        self,
        *,
        latest_block: int,
        last_processed: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Drain a stale checkpoint to the confirmed head in bounded ranges.

        Historical chunks run back-to-back rather than waiting the live polling
        interval between them. ``should_stop`` is checked at chunk boundaries so
        shutdown is prompt without abandoning a partially processed range.
        """

        safe_block = latest_block - self.confirmation_blocks
        while last_processed < safe_block:
            if should_stop is not None and should_stop():
                break

            # MAX_BLOCK_RANGE still protects RPC providers from oversized log
            # queries. The live POLL_INTERVAL must not run between these
            # historical chunks: doing so made restart latency grow by five
            # seconds for every 50 blocks and delayed new claims for minutes.
            last_processed = self.process_latest(
                latest_block=latest_block,
                last_processed=last_processed,
            )

        return last_processed


def main() -> None:
    """Run the production listener until an operating-system shutdown signal.

    Startup fails before polling if the replay origin, deployment identity,
    contract bytecode, or required adapters are invalid. The first checkpoint is
    one block before ``LISTENER_START_BLOCK`` so the deployment block itself is
    included. Runtime adapter failures are logged and retried from PostgreSQL;
    the Kafka producer is flushed and closed during orderly shutdown.
    """

    configure_logging("claims-listener")
    # Metrics contain no claimant data. The Ops Agent reads this private
    # endpoint on the VM and forwards the samples to Cloud Monitoring.
    metrics = ListenerMetrics.start_from_env()
    shutdown = ShutdownSignal()
    shutdown.install()

    try:
        configured_start_block = required_listener_start_block(os.environ)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    try:
        deployment = load_claims_deployment(os.environ)
    except DeploymentConfigurationError as exc:
        raise SystemExit(str(exc)) from exc
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    # If Sepolia reports an extraData validation error, inject
    # ExtraDataToPOAMiddleware here before the connection check.
    try:
        contract = connect_claims_deployment(w3, deployment)
    except DeploymentValidationError as exc:
        raise SystemExit(str(exc)) from exc
    contract_address = deployment.address
    kafka_settings = KafkaSettings.from_env()
    claim_event_publisher = create_publisher(kafka_settings)
    repositories = PostgresRepositories.from_env()
    chain_id = deployment.chain_id
    _, dead_letter_path = deployment_state_paths(
        os.environ,
        deployment_id=deployment.deployment_id,
        chain_id=chain_id,
        contract_address=contract_address,
    )
    cursor = PostgresClaimIndexCheckpoint(
        repositories.claims,
        chain_id=chain_id,
        contract_address=contract_address,
    )
    processor = ClaimEventProcessor(
        chain_id=chain_id,
        contract_address=contract_address,
        contract=contract,
        ipfs=IPFSClient.from_env(),
        publisher=claim_event_publisher,
        indexer=repositories.claims,
        metrics=metrics,
        dead_letter=JsonlDeadLetterSink(dead_letter_path),
    )
    poller = ConfirmedBlockPoller(
        processor=processor,
        checkpoint=cursor,
        confirmation_blocks=CONFIRMATION_BLOCKS,
        max_block_range=MAX_BLOCK_RANGE,
    )

    logger.info(
        "listener.started",
        event_names=list(processor.event_names),
        deployment_id=deployment.deployment_id,
        chain_id=chain_id,
        contract_address=contract_address,
    )
    if claim_event_publisher is not None:
        logger.info(
            "kafka.publisher_enabled",
            topic=kafka_settings.topic,
            bootstrap_servers=kafka_settings.bootstrap_servers,
        )

    first_run_default = configured_start_block - 1
    last_processed = cursor.load(default=first_run_default)
    logger.info(
        "listener.checkpoint_loaded",
        checkpoint_store="postgresql",
        last_processed_block=last_processed,
    )
    logger.info("listener.dead_letter_ready", path=str(dead_letter_path))

    try:
        while not shutdown.is_set():
            try:
                latest = w3.eth.block_number
                last_processed = poller.process_to_safe_head(
                    latest_block=latest,
                    last_processed=last_processed,
                    should_stop=shutdown.is_set,
                )
                metrics.observe_poll(
                    latest_block=latest,
                    last_processed_block=last_processed,
                    confirmation_blocks=CONFIRMATION_BLOCKS,
                )
            except Exception as exc:  # noqa: BLE001 - adapter failures retry safely
                # RPC, IPFS and Kafka failures all retry from the saved checkpoint.
                metrics.observe_poll_error()
                logger.warning(
                    "listener.poll_failed",
                    error_type=type(exc).__name__,
                    retrying=True,
                )
            # Unlike time.sleep, this returns immediately when Docker asks the
            # container to stop, making normal deployments quick and predictable.
            shutdown.wait(POLL_INTERVAL)
    finally:
        if claim_event_publisher is not None:
            claim_event_publisher.close()


if __name__ == "__main__":
    main()
