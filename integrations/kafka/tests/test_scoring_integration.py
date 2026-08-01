"""Broker-and-database integration test for the asynchronous scoring workflow."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from web3 import Web3

from backend.app.blockchain import ChainAssessment, ChainClaim
from backend.app.models import ClaimSubmission
from backend.app.submission_auth import ClaimAuthorizationSigner, InsurerPrincipal
from duplicates import CrossInsurerDuplicateDetector
from integrations.kafka import (
    ClaimSubmittedEvent,
    KafkaClaimEventConsumer,
    KafkaClaimEventPublisher,
)
from integrations.kafka.scoring_worker import ClaimScoringHandler
from integrations.postgres import ClaimFeatureProcessor
from model.contracts import FraudReason, FraudScore

pytestmark = pytest.mark.integration
AUTHORIZATION = ClaimAuthorizationSigner(
    b"kafka-integration-authorization-key-32-bytes"
)


class PayloadStore:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes:
        assert attempts == 3
        return self.payloads[pointer]


class DeterministicScorer:
    def score(self, _claim) -> FraudScore:
        return FraudScore(
            probability=0.42,
            score_basis_points=4200,
            threshold=0.47,
            flagged=False,
            model_version="integration-model-v1",
            reasons=(FraudReason("claim_amount_usd", "Claim amount", 0.1),),
        )


@dataclass
class RegistryState:
    status: int = 0
    fraud_score: int = 0


class InMemoryRegistry:
    def __init__(self) -> None:
        self.states: dict[int, RegistryState] = {}

    def get_claim(self, claim_id: int) -> ChainClaim:
        state = self.states.setdefault(claim_id, RegistryState())
        return ChainClaim(
            claim_id=claim_id,
            claimant="0x2222222222222222222222222222222222222222",
            claim_hash="0xhash",
            data_pointer=f"ipfs://claim-{claim_id}",
            status=state.status,
            fraud_score=state.fraud_score,
            submitted_at=1_750_000_000,
            updated_at=1_750_000_000,
        )

    def assess_claim(
        self,
        claim_id: int,
        status: int,
        fraud_score: int,
    ) -> ChainAssessment:
        self.states[claim_id] = RegistryState(status, fraud_score)
        return ChainAssessment(
            transaction_hash=f"0xassessment-{claim_id}",
            block_number=200 + claim_id,
            status=status,
            fraud_score=fraud_score,
        )


def claim_payload(insurer_id: str) -> bytes:
    claim = ClaimSubmission(
        insurerId=insurer_id,
        claimReference=f"{insurer_id}-claim",
        policyReference=f"{insurer_id}-policy",
        claimType="collision",
        incidentDate="2026-07-13",
        claimAmountUsd=2500,
        policyPremiumUsd=480,
        vehicleAge=6,
        vehicleType="sedan",
        country="Nigeria",
        regionType="urban",
        thirdPartyInjuryFlag=False,
        totalLossFlag=False,
        description=f"Synthetic incident described by {insurer_id}",
        evidence=[],
    )
    principal = InsurerPrincipal(
        insurer_id=insurer_id,
        credential_id=f"{insurer_id}-integration-v1",
        permitted_operations=frozenset({"submit_claim"}),
        daily_quota=25,
    )
    return AUTHORIZATION.authorized_claim_bytes(claim, principal)


def submitted_event(claim_id: int, payload: bytes) -> ClaimSubmittedEvent:
    transaction_hash = f"0x{claim_id:064x}"
    event_timestamp = int(datetime(2026, 7, 20, tzinfo=UTC).timestamp())
    return ClaimSubmittedEvent.create(
        chain_id=11_155_111,
        contract_address="0x1111111111111111111111111111111111111111",
        claim_id=claim_id,
        claimant="0x2222222222222222222222222222222222222222",
        claim_hash=Web3.keccak(payload).hex(),
        data_pointer=f"ipfs://claim-{claim_id}",
        block_number=100 + claim_id,
        block_hash=f"0x{(100 + claim_id):064x}",
        transaction_hash=transaction_hash,
        log_index=0,
        event_timestamp=event_timestamp + claim_id,
    )


def test_broker_events_are_scored_and_matched_across_insurers(
    kafka_settings,
    postgres_repository,
):
    payloads = {
        "ipfs://claim-7": claim_payload("northstar-mutual"),
        "ipfs://claim-8": claim_payload("harbour-shield"),
    }
    registry = InMemoryRegistry()
    handler = ClaimScoringHandler(
        ipfs=PayloadStore(payloads),
        scorer=DeterministicScorer(),
        duplicate_detector=CrossInsurerDuplicateDetector(
            b"kafka-integration-key-" * 2,
            postgres_repository,
        ),
        feature_processor=ClaimFeatureProcessor(
            b"kafka-integration-key-" * 2,
            postgres_repository,
        ),
        repository=postgres_repository,
        registry=registry,
        authorization=AUTHORIZATION,
    )
    publisher = KafkaClaimEventPublisher(kafka_settings)
    consumer = KafkaClaimEventConsumer(kafka_settings)
    events = [
        submitted_event(7, payloads["ipfs://claim-7"]),
        submitted_event(8, payloads["ipfs://claim-8"]),
    ]
    try:
        for event in events:
            publisher.publish(event)

        processed = 0
        deadline = time.monotonic() + 20
        while processed < len(events) and time.monotonic() < deadline:
            processed += int(consumer.process_next(handler, timeout=1))
    finally:
        publisher.close()
        consumer.close()

    assert processed == 2
    first_record = postgres_repository.get_by_event_id(events[0].event_id)
    second_record = postgres_repository.get_by_event_id(events[1].event_id)
    assert first_record is not None
    assert second_record is not None
    assert first_record.processing_status == "completed"
    assert second_record.processing_status == "completed"

    first_features = postgres_repository.get_feature_snapshot(events[0].event_id)
    second_features = postgres_repository.get_feature_snapshot(events[1].event_id)
    assert first_features is not None
    assert second_features is not None
    assert first_features.report_delay_days == 7
    # These claims can land on different Kafka partitions, so either may be
    # processed first. Exactly the later processed snapshot observes the match.
    assert sorted(
        [
            first_features.cross_insurer_duplicate_match_count,
            second_features.cross_insurer_duplicate_match_count,
        ]
    ) == [0, 1]

    scope = {
        "chain_id": 11_155_111,
        "contract_address": "0x1111111111111111111111111111111111111111",
    }
    first = postgres_repository.get_duplicate_check_for_claim(
        **scope, claim_id=7
    )
    second = postgres_repository.get_duplicate_check_for_claim(
        **scope, claim_id=8
    )
    assert first is not None
    assert second is not None
    assert [(match.claim_id, match.insurer_id) for match in first.matches] == [
        (8, "harbour-shield")
    ]
    assert [(match.claim_id, match.insurer_id) for match in second.matches] == [
        (7, "northstar-mutual")
    ]
