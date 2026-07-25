"""Turn one verified Kafka event into one repeatable model assessment.

The worker treats Kafka delivery as at-least-once: a message may return after a
restart, so every step is written to be safe on replay. PostgreSQL remembers the
score and Sepolia remains the public lifecycle record. The Kafka offset is
committed only after this handler returns successfully.
"""

from __future__ import annotations

from typing import Protocol

from web3 import Web3

from backend.app.blockchain import (
    ChainAssessment,
    ChainClaim,
    SepoliaClaimsRegistry,
)
from backend.app.models import StoredClaimDocument
from integrations.ipfs import IPFSClient
from integrations.postgres import AssessmentRecord, PostgresAssessmentRepository
from model.scorer import FraudScore
from model.xgboost_scorer import XGBoostFraudScorer

from .events import ClaimSubmittedEvent, KafkaClaimEventConsumer, KafkaSettings


class ClaimReader(Protocol):
    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes: ...


class ClaimScorer(Protocol):
    def score(self, claim: StoredClaimDocument) -> FraudScore: ...


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


class ClaimScoringHandler:
    """Own the complete event-to-assessment workflow behind one callable."""

    def __init__(
        self,
        *,
        ipfs: ClaimReader,
        scorer: ClaimScorer,
        repository: AssessmentStore,
        registry: AssessmentRegistry,
    ) -> None:
        self.ipfs = ipfs
        self.scorer = scorer
        self.repository = repository
        self.registry = registry

    def __call__(self, event: ClaimSubmittedEvent) -> None:
        existing = self.repository.get_by_event_id(event.event_id)
        if existing and existing.processing_status == "completed":
            # Kafka may redeliver a committed event after maintenance or an
            # offset reset. A completed database record makes that a cheap no-op.
            print(
                f"[AssessmentAlreadyCompleted] eventId={event.event_id} "
                f"claimId={event.claim_id}"
            )
            return

        payload = self.ipfs.download_pointer(event.data_pointer)
        verify_claim_payload(event, payload)
        # Parse only after the hash check. This prevents a different document at
        # the same external URL from ever reaching feature extraction.
        claim = StoredClaimDocument.model_validate_json(payload)

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

        print(
            f"[ClaimAssessed] eventId={event.event_id} claimId={event.claim_id} "
            f"model={record.model_version} score={record.fraud_score}"
        )


def main() -> None:
    settings = KafkaSettings.from_env()
    if not settings.enabled:
        raise SystemExit("Set KAFKA_ENABLED=true before starting the scoring worker")

    repository = PostgresAssessmentRepository.from_env()
    repository.ensure_schema()
    handler = ClaimScoringHandler(
        ipfs=IPFSClient.from_env(),
        scorer=XGBoostFraudScorer.from_env(),
        repository=repository,
        registry=SepoliaClaimsRegistry.from_env(),
    )
    consumer = KafkaClaimEventConsumer(settings)
    print(
        f"Scoring {settings.topic} from {settings.bootstrap_servers} "
        f"as group {settings.consumer_group_id}"
    )
    try:
        while True:
            consumer.process_next(handler)
    except KeyboardInterrupt:
        print("Stopping scoring worker")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
