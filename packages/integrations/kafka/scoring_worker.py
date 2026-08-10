"""Turn one verified Kafka event into one repeatable model assessment.

The worker treats Kafka delivery as at-least-once: a message may return after a
restart, so every step is written to be safe on replay. PostgreSQL remembers the
score and Sepolia remains the public lifecycle record. The Kafka offset is
committed only after this handler returns successfully.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError
from web3 import Web3

from apps.backend.app.blockchain import (
    ChainAssessment,
    ChainClaim,
    SepoliaClaimsRegistry,
)
from apps.backend.app.models import StoredClaimDocument
from apps.backend.app.submission_auth import (
    ClaimAuthorizationSigner,
    ClaimAuthorizationVerificationError,
    InsurerPrincipal,
)
from packages.duplicates import CrossInsurerDuplicateDetector, DuplicateCheck
from packages.integrations.ipfs import IPFSClient
from packages.integrations.postgres import (
    AssessmentRecord,
    ClaimFeatureProcessor,
    ClaimFeatureSnapshot,
    PostgresRepositories,
)
from packages.model.contracts import FraudScore
from packages.model.xgboost_scorer import XGBoostFraudScorer
from packages.observability import (
    ScoringMetrics,
    ShutdownSignal,
    configure_logging,
    get_event_logger,
)

from .events import ClaimSubmittedEvent, KafkaClaimEventConsumer, KafkaSettings

logger = get_event_logger(__name__)


class PermanentClaimProcessingError(RuntimeError):
    """An immutable claim defect that will produce the same result on replay.

    This marker is intentionally narrow. Only errors caused by bytes or public
    identities that are already anchored on-chain belong here. Network, Kafka,
    database, IPFS availability, model, and Sepolia errors must keep propagating
    normally so Kafka leaves the offset uncommitted and retries them later.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        """Attach a stable machine code without retaining private input values."""

        super().__init__(message)
        self.reason_code = reason_code


class ClaimDeadLetterSink(Protocol):
    """Durably retain public metadata for a permanently rejected claim event."""

    def record(
        self,
        event: ClaimSubmittedEvent,
        error: PermanentClaimProcessingError,
    ) -> None:
        """Return only after the rejection is safe for Kafka to commit past."""

        ...


def scoring_dead_letter_path(
    settings: Mapping[str, str],
    *,
    deployment_id: str,
) -> Path:
    """Choose a deployment-scoped operational file for rejected claim events.

    A worker can be repointed at another registry while keeping the same local
    state volume. Including the validated deployment ID in the default filename
    prevents those independent audit streams from being mixed accidentally.
    Operators may still provide an explicit file path for managed mounts.
    """

    state_dir = Path(
        settings.get(
            "SCORING_STATE_DIR",
            str(Path(__file__).with_name(".state")),
        )
    )
    return Path(
        settings.get(
            "SCORING_DEAD_LETTER_FILE",
            str(state_dir / f"{deployment_id}-dead-letter.jsonl"),
        )
    )


class JsonlClaimDeadLetterSink:
    """Append sanitized rejection records to a durable, operator-readable file.

    JSON Lines keeps every rejection independently readable and works with the
    single-VM deployment without adding a second Kafka producer or database
    dependency to the failure path. The full IPFS claim is deliberately absent:
    blockchain coordinates are sufficient to investigate or replay the event,
    while copying claim contents would create another sensitive-data store.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(
        self,
        event: ClaimSubmittedEvent,
        error: PermanentClaimProcessingError,
    ) -> None:
        """Flush one rejection to disk before the handler allows an offset commit.

        If directory creation, writing, flushing, or ``fsync`` fails, the error
        is allowed to escape. That fail-closed behavior is important: Kafka must
        replay the event rather than silently skipping a claim whose rejection
        was never durably recorded.

        A crash after this fsync but before Kafka's commit can append the same
        event again on restart. That is expected under at-least-once delivery;
        the deterministic ``eventId`` lets operators identify such duplicates.
        """

        entry: dict[str, Any] = {
            "recordedAt": datetime.now(UTC).isoformat(),
            "reasonCode": error.reason_code,
            "reason": str(error),
            # Everything below already appears in the public blockchain event.
            # No insurer API key, authorization signature, or IPFS bytes enter
            # this operational file.
            "eventId": event.event_id,
            "chainId": event.chain_id,
            "contractAddress": event.contract_address,
            "claimId": event.claim_id,
            "blockNumber": event.block_number,
            "transactionHash": event.transaction_hash,
            "logIndex": event.log_index,
            "dataPointer": event.data_pointer,
        }
        serialized = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_already_existed = self.path.exists()
        with self.path.open("a", encoding="utf-8") as dead_letter_file:
            dead_letter_file.write(serialized)
            dead_letter_file.flush()
            os.fsync(dead_letter_file.fileno())
        if not file_already_existed:
            # The first file fsync persists its contents, while fsyncing the
            # parent directory persists the new filename itself. Without the
            # directory sync, a sudden VM loss could theoretically leave a
            # committed Kafka offset but no directory entry for the new file.
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)


class QuarantiningClaimHandler:
    """Turn a durably recorded permanent rejection into handler success.

    ``KafkaClaimEventConsumer`` commits only when its handler returns normally.
    Therefore this wrapper sits around the real scorer: it suppresses an error
    only after the dead-letter sink succeeds. All unmarked exceptions continue
    upward unchanged, preserving retries for temporary infrastructure failures.
    """

    def __init__(
        self,
        handler: ClaimEventHandler,
        *,
        dead_letter: ClaimDeadLetterSink,
        metrics: ScoringMetrics | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.handler = handler
        self.dead_letter = dead_letter
        self.metrics = metrics
        self.clock = clock

    def __call__(self, event: ClaimSubmittedEvent) -> None:
        started_at = self.clock()
        try:
            self.handler(event)
        except PermanentClaimProcessingError as exc:
            # The ordering is the safety property: first persist, then return.
            # Returning lets the existing consumer commit this exact message;
            # writing after return could lose the only rejection record.
            self.dead_letter.record(event, exc)
            if self.metrics is not None:
                # Count quarantine only after the fsync above succeeds. This
                # makes the metric mean that durable evidence exists and the
                # consumer can safely commit, not merely that validation failed.
                self.metrics.observe_handled(
                    outcome="quarantined",
                    duration_seconds=self.clock() - started_at,
                )
            logger.warning(
                "claim.quarantined",
                event_id=event.event_id,
                claim_id=event.claim_id,
                transaction_hash=event.transaction_hash,
                reason_code=exc.reason_code,
            )


class ClaimReader(Protocol):
    """Read the exact public bytes referenced by a confirmed chain event."""

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes:
        """Download an IPFS pointer with bounded dependency retries."""

        ...


class ClaimScorer(Protocol):
    """Produce the versioned fraud score consumed by the workflow."""

    def score(self, claim: StoredClaimDocument) -> FraudScore:
        """Score one verified stored claim deterministically."""

        ...


class ClaimAuthorizationVerifier(Protocol):
    """Recover the API-authenticated identity embedded in IPFS bytes."""

    def verify_claim(self, claim: StoredClaimDocument) -> InsurerPrincipal:
        """Verify gateway authorization and return its insurer principal."""

        ...


class ClaimEventHandler(Protocol):
    """Callable boundary used by the Kafka consumer loop."""

    def __call__(self, event: ClaimSubmittedEvent) -> None:
        """Handle one decoded chain-reference event and validate its IPFS claim."""

        ...


class DuplicateDetector(Protocol):
    """Check privacy-preserving incident identity against prior insurers."""

    def check(
        self,
        event: ClaimSubmittedEvent,
        claim: StoredClaimDocument,
    ) -> DuplicateCheck:
        """Return cross-insurer matches without exposing raw policy identity."""

        ...


class FeatureProcessor(Protocol):
    """Persist a versioned, replay-safe feature snapshot for one claim."""

    def process(
        self,
        event: ClaimSubmittedEvent,
        claim: StoredClaimDocument,
        duplicate_check: DuplicateCheck,
    ) -> ClaimFeatureSnapshot:
        """Return the exact persisted feature version used for this event."""

        ...


class AssessmentStore(Protocol):
    """Durable assessment state used to make Kafka redelivery idempotent."""

    def get_by_event_id(self, event_id: str) -> AssessmentRecord | None:
        """Return the prior score/write state for one immutable event ID."""

        ...

    def save_scored(self, record: AssessmentRecord) -> None:
        """Persist model output before attempting the Sepolia write."""

        ...

    def mark_completed(
        self,
        event_id: str,
        *,
        transaction_hash: str | None,
        block_number: int | None,
    ) -> None:
        """Persist successful or reconciled on-chain assessment completion."""

        ...

    def mark_failed(self, event_id: str, error: str) -> None:
        """Record the latest failed write attempt for operational diagnosis."""

        ...


class AssessmentRegistry(Protocol):
    """Minimal chain interface needed for idempotent assessment write-back."""

    def get_claim(self, claim_id: int) -> ChainClaim:
        """Read authoritative lifecycle state before attempting an update."""

        ...

    def assess_claim(
        self,
        claim_id: int,
        status: int,
        fraud_score: int,
    ) -> ChainAssessment:
        """Write one allowed model outcome and wait for its receipt."""

        ...


def verify_claim_payload(event: ClaimSubmittedEvent, payload: bytes) -> None:
    """Refuse to score bytes that do not match the public on-chain commitment.

    Hash mismatch deliberately remains an unmarked, retryable error here. The
    listener already verified the immutable pointer before publishing, so a
    later mismatch can indicate a temporary gateway/cache response rather than
    a malformed claim. Quarantining it immediately could skip data that a later
    clean IPFS response would successfully verify.
    """

    actual_hash = Web3.keccak(payload).hex().removeprefix("0x").lower()
    expected_hash = event.claim_hash.removeprefix("0x").lower()
    if actual_hash != expected_hash:
        raise ValueError(f"IPFS hash does not match for Kafka event {event.event_id}")


class MonitoredScorer:
    """Measure model work while preserving the scorer's small public interface."""

    def __init__(
        self,
        scorer: ClaimScorer,
        metrics: ScoringMetrics,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """Wrap a scorer with model-only latency and result metrics."""

        self.scorer = scorer
        self.metrics = metrics
        self.clock = clock

    def score(self, claim: StoredClaimDocument) -> FraudScore:
        """Run XGBoost and SHAP, then record timing and non-sensitive results."""

        started_at = self.clock()
        result = self.scorer.score(claim)
        self.metrics.observe_inference(
            duration_seconds=self.clock() - started_at,
            probability=result.probability,
            fraud_score=result.score_basis_points,
        )
        return result


class MonitoredClaimHandler:
    """Measure completed and retryable-failure handler calls.

    Permanent rejection metrics belong to ``QuarantiningClaimHandler`` because
    only that outer boundary knows whether durable quarantine actually succeeded.
    """

    def __init__(
        self,
        handler: ClaimEventHandler,
        metrics: ScoringMetrics,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """Wrap scoring with completed/retryable-failure duration metrics."""

        self.handler = handler
        self.metrics = metrics
        self.clock = clock

    def __call__(self, event: ClaimSubmittedEvent) -> None:
        """Measure the whole operation, including database and Sepolia work."""

        started_at = self.clock()
        try:
            self.handler(event)
        except PermanentClaimProcessingError:
            # The outer QuarantiningClaimHandler records this outcome only after
            # its durable dead-letter write succeeds. Counting it here would
            # claim success even when that operations volume is unavailable.
            raise
        except Exception:
            self.metrics.observe_handled(
                outcome="failed",
                duration_seconds=self.clock() - started_at,
            )
            raise

        # A previously completed, replayed event is also a successful outcome:
        # the idempotency protection handled it exactly as designed.
        self.metrics.observe_handled(
            outcome="completed",
            duration_seconds=self.clock() - started_at,
        )


class ClaimScoringHandler:
    """Own the complete event-to-assessment workflow behind one callable."""

    def __init__(
        self,
        *,
        ipfs: ClaimReader,
        scorer: ClaimScorer,
        duplicate_detector: DuplicateDetector,
        feature_processor: FeatureProcessor,
        repository: AssessmentStore,
        registry: AssessmentRegistry,
        authorization: ClaimAuthorizationVerifier,
    ) -> None:
        """Inject every external boundary in the event-to-assessment pipeline."""

        self.ipfs = ipfs
        self.scorer = scorer
        self.duplicate_detector = duplicate_detector
        self.feature_processor = feature_processor
        self.repository = repository
        self.registry = registry
        self.authorization = authorization

    def __call__(self, event: ClaimSubmittedEvent) -> None:
        """Verify, enrich, score, and idempotently write one confirmed claim.

        Chain hash and gateway authorization are checked before model input.
        Model output is persisted before Sepolia submission so a retry reuses the
        exact decision. Existing matching chain state repairs crash gaps without
        submitting a second assessment transaction.
        """

        existing = self.repository.get_by_event_id(event.event_id)
        if existing and existing.processing_status == "completed":
            # Kafka may redeliver a committed event after maintenance or an
            # offset reset. A completed database record makes that a cheap no-op.
            logger.info(
                "assessment.already_completed",
                event_id=event.event_id,
                claim_id=event.claim_id,
                transaction_hash=existing.transaction_hash,
            )
            return

        payload = self.ipfs.download_pointer(event.data_pointer)
        verify_claim_payload(event, payload)
        # Parse only after the hash check. This prevents a different document at
        # the same external URL from ever reaching feature extraction.
        try:
            claim = StoredClaimDocument.model_validate_json(payload)
        except ValidationError as exc:
            # The bytes and their hash are already permanent on Sepolia. A
            # missing field, invalid type, unsupported schema version, or broken
            # JSON document will therefore fail identically on every replay.
            # Do not include Pydantic's full error here because it can echo input
            # values into logs and the dead-letter operations file.
            raise PermanentClaimProcessingError(
                "invalid_claim_schema",
                "Claim document does not match the supported stored-claim schema",
            ) from exc

        try:
            principal = self.authorization.verify_claim(claim)
        except ClaimAuthorizationVerificationError as exc:
            # Gateway authorization is embedded in the immutable IPFS bytes. A
            # missing or invalid signature cannot be repaired by waiting for an
            # external service, so it is safe to quarantine rather than retry.
            raise PermanentClaimProcessingError(
                "invalid_claim_authorization",
                "Claim document does not contain valid gateway authorization",
            ) from exc
        if principal.insurer_id != claim.insurer_id:
            raise PermanentClaimProcessingError(
                "insurer_identity_mismatch",
                "Authorized insurer identity does not match the claim",
            )
        if principal.signer_address.lower() != event.claimant.lower():
            raise PermanentClaimProcessingError(
                "claimant_identity_mismatch",
                "Authorized insurer signer does not match the on-chain claimant",
            )
        duplicate_check = self.duplicate_detector.check(event, claim)
        feature_snapshot = self.feature_processor.process(
            event,
            claim,
            duplicate_check,
        )

        record = existing
        if record is None:
            score = self.scorer.score(claim)
            record = AssessmentRecord.from_score(
                event_id=event.event_id,
                chain_id=event.chain_id,
                contract_address=event.contract_address,
                claim_id=event.claim_id,
                score=score,
            )
            # Save the exact probability, threshold, and SHAP reasons before the
            # chain write. A retry then reuses the original decision rather than
            # silently rescoring with a changed model artifact.
            self.repository.save_scored(record)

        # The automated model has only two allowed outcomes. UnderReview (1) and
        # Flagged (4) both still require a person; Approved/Rejected are never
        # inferred from a probability.
        desired_status = 4 if record.status == "Flagged" else 1
        assessment_transaction_hash = record.transaction_hash
        try:
            chain_claim = self.registry.get_claim(event.claim_id)
            if chain_claim.status == 0:
                # Submitted is the only state the worker is allowed to assess.
                # Any later human or model state is checked rather than overwritten.
                assessment = self.registry.assess_claim(
                    event.claim_id,
                    desired_status,
                    record.fraud_score,
                )
                self.repository.mark_completed(
                    event.event_id,
                    transaction_hash=assessment.transaction_hash,
                    block_number=assessment.block_number,
                )
                assessment_transaction_hash = assessment.transaction_hash
            elif (
                chain_claim.status == desired_status
                and chain_claim.fraud_score == record.fraud_score
            ):
                # A crash can happen after the chain write but before the database
                # update. Matching chain state makes that replay safe.
                self.repository.mark_completed(
                    event.event_id,
                    transaction_hash=record.transaction_hash,
                    block_number=record.block_number,
                )
            else:
                raise ValueError(
                    f"Claim {event.claim_id} already has a different assessment"
                )
        except Exception as exc:
            self.repository.mark_failed(event.event_id, str(exc))
            raise

        logger.info(
            "claim.assessed",
            event_id=event.event_id,
            claim_id=event.claim_id,
            source_transaction_hash=event.transaction_hash,
            assessment_transaction_hash=assessment_transaction_hash,
            model_version=record.model_version,
            fraud_score=record.fraud_score,
            feature_version=feature_snapshot.feature_version,
            cross_insurer_matches=len(duplicate_check.matches),
        )


def main() -> None:
    """Run the at-least-once Kafka scoring loop until graceful shutdown.

    Offsets advance only after the monitored handler returns successfully;
    PostgreSQL and chain-state checks make redelivery safe after failure.
    """

    configure_logging("claims-scoring-worker")
    metrics = ScoringMetrics.start_from_env()
    shutdown = ShutdownSignal()
    shutdown.install()

    settings = KafkaSettings.from_env()
    if not settings.enabled:
        raise SystemExit("Set KAFKA_ENABLED=true before starting the scoring worker")

    repositories = PostgresRepositories.from_env()
    # Keep model-only latency separate from total pipeline time. Blockchain
    # confirmation can take seconds, so combining both would make a 500 ms
    # inference target impossible to interpret fairly.
    scorer = MonitoredScorer(XGBoostFraudScorer.from_env(), metrics)
    # Construct the registry once so its validated deployment identity can also
    # scope the dead-letter filename. The worker must never write rejections for
    # two independent contracts into one ambiguous operations file.
    registry = SepoliaClaimsRegistry.from_env(
        private_key_env="SEPOLIA_ASSESSOR_PRIVATE_KEY"
    )
    handler = ClaimScoringHandler(
        ipfs=IPFSClient.from_env(),
        scorer=scorer,
        duplicate_detector=CrossInsurerDuplicateDetector.from_env(
            repositories.duplicates
        ),
        feature_processor=ClaimFeatureProcessor.from_env(repositories.features),
        repository=repositories.assessments,
        registry=registry,
        authorization=ClaimAuthorizationSigner.from_env(),
    )
    monitored_handler = MonitoredClaimHandler(handler, metrics)
    dead_letter_file = scoring_dead_letter_path(
        os.environ,
        deployment_id=registry.deployment.deployment_id,
    )
    # The outer wrapper returns normally only for a permanent error that has
    # already been fsync'd to the operations file. The unchanged consumer then
    # commits that message and can read the next claim in the partition.
    partition_safe_handler = QuarantiningClaimHandler(
        monitored_handler,
        dead_letter=JsonlClaimDeadLetterSink(dead_letter_file),
        metrics=metrics,
    )
    consumer = KafkaClaimEventConsumer(settings)
    logger.info(
        "scoring_worker.started",
        topic=settings.topic,
        bootstrap_servers=settings.bootstrap_servers,
        consumer_group_id=settings.consumer_group_id,
        dead_letter_file=str(dead_letter_file),
    )
    try:
        while not shutdown.is_set():
            consumer.process_next(partition_safe_handler)
    except KeyboardInterrupt:
        # This remains as a defensive fallback for platforms that deliver a
        # KeyboardInterrupt before our SIGINT handler has been installed.
        logger.info("scoring_worker.stopping", reason="keyboard_interrupt")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
