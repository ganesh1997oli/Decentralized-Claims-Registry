"""Turn one verified Kafka event into one repeatable model assessment.

The worker treats Kafka delivery as at-least-once: a message may return after a
restart, so every step is written to be safe on replay. PostgreSQL remembers the
score and Sepolia remains the public lifecycle record. The Kafka offset is
committed only after this handler returns successfully.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from web3 import Web3

from apps.backend.app.blockchain import (
    ChainAssessment,
    ChainClaim,
    SepoliaClaimsRegistry,
)
from apps.backend.app.models import StoredClaimDocument
from apps.backend.app.submission_auth import (
    ClaimAuthorizationSigner,
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


class ClaimReader(Protocol):
    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes: ...


class ClaimScorer(Protocol):
    def score(self, claim: StoredClaimDocument) -> FraudScore: ...


class ClaimAuthorizationVerifier(Protocol):
    def verify_claim(self, claim: StoredClaimDocument) -> InsurerPrincipal: ...


class ClaimEventHandler(Protocol):
    def __call__(self, event: ClaimSubmittedEvent) -> None: ...


class DuplicateDetector(Protocol):
    def check(
        self,
        event: ClaimSubmittedEvent,
        claim: StoredClaimDocument,
    ) -> DuplicateCheck: ...


class FeatureProcessor(Protocol):
    def process(
        self,
        event: ClaimSubmittedEvent,
        claim: StoredClaimDocument,
        duplicate_check: DuplicateCheck,
    ) -> ClaimFeatureSnapshot: ...


class AssessmentStore(Protocol):
    def get_by_event_id(self, event_id: str) -> AssessmentRecord | None: ...

    def save_scored(self, record: AssessmentRecord) -> None: ...

    def mark_completed(
        self,
        event_id: str,
        *,
        transaction_hash: str | None,
        block_number: int | None,
    ) -> None: ...

    def mark_failed(self, event_id: str, error: str) -> None: ...


class AssessmentRegistry(Protocol):
    def get_claim(self, claim_id: int) -> ChainClaim: ...

    def assess_claim(
        self,
        claim_id: int,
        status: int,
        fraud_score: int,
    ) -> ChainAssessment: ...


def verify_claim_payload(event: ClaimSubmittedEvent, payload: bytes) -> None:
    """Refuse to score bytes that do not match the public on-chain commitment."""

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
    """Count completed and failed Kafka handler calls in one reliable place."""

    def __init__(
        self,
        handler: ClaimEventHandler,
        metrics: ScoringMetrics,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.handler = handler
        self.metrics = metrics
        self.clock = clock

    def __call__(self, event: ClaimSubmittedEvent) -> None:
        """Measure the whole operation, including database and Sepolia work."""

        started_at = self.clock()
        try:
            self.handler(event)
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
        self.ipfs = ipfs
        self.scorer = scorer
        self.duplicate_detector = duplicate_detector
        self.feature_processor = feature_processor
        self.repository = repository
        self.registry = registry
        self.authorization = authorization

    def __call__(self, event: ClaimSubmittedEvent) -> None:
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
        claim = StoredClaimDocument.model_validate_json(payload)
        principal = self.authorization.verify_claim(claim)
        if principal.insurer_id != claim.insurer_id:
            raise ValueError("Authorized insurer identity does not match the claim")
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
    handler = ClaimScoringHandler(
        ipfs=IPFSClient.from_env(),
        scorer=scorer,
        duplicate_detector=CrossInsurerDuplicateDetector.from_env(
            repositories.duplicates
        ),
        feature_processor=ClaimFeatureProcessor.from_env(repositories.features),
        repository=repositories.assessments,
        registry=SepoliaClaimsRegistry.from_env(
            private_key_env="SEPOLIA_ASSESSOR_PRIVATE_KEY"
        ),
        authorization=ClaimAuthorizationSigner.from_env(),
    )
    monitored_handler = MonitoredClaimHandler(handler, metrics)
    consumer = KafkaClaimEventConsumer(settings)
    logger.info(
        "scoring_worker.started",
        topic=settings.topic,
        bootstrap_servers=settings.bootstrap_servers,
        consumer_group_id=settings.consumer_group_id,
    )
    try:
        while not shutdown.is_set():
            consumer.process_next(monitored_handler)
    except KeyboardInterrupt:
        # This remains as a defensive fallback for platforms that deliver a
        # KeyboardInterrupt before our SIGINT handler has been installed.
        logger.info("scoring_worker.stopping", reason="keyboard_interrupt")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
