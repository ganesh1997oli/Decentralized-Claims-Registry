"""Broker-and-database integration test for the asynchronous scoring workflow."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
import time
from uuid import uuid4

from confluent_kafka.admin import AdminClient, NewTopic
import pytest
from web3 import Web3

from backend.app.blockchain import ChainAssessment, ChainClaim
from backend.app.models import ClaimSubmission
from duplicates import CrossInsurerDuplicateDetector
from integrations.kafka import (
    ClaimSubmittedEvent,
    KafkaClaimEventConsumer,
    KafkaClaimEventPublisher,
    KafkaSettings,
)
from integrations.kafka.scoring_worker import ClaimScoringHandler
from integrations.postgres import ClaimFeatureProcessor
from model.contracts import FraudReason, FraudScore


pytestmark = pytest.mark.integration


@pytest.fixture
def kafka_settings():
    """Create an isolated topic and consumer group on an explicit test broker."""

    bootstrap_servers = os.environ.get(
        "TEST_KAFKA_BOOTSTRAP_SERVERS",
        "",
    ).strip()
    if not bootstrap_servers:
        pytest.skip(
            "set TEST_KAFKA_BOOTSTRAP_SERVERS to run Kafka integration tests"
        )

    identity = uuid4().hex
    topic = f"claims.integration.{identity}"
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    create_future = admin.create_topics(
        [NewTopic(topic, num_partitions=2, replication_factor=1)]
    )[topic]
    create_future.result(timeout=15)
    settings = KafkaSettings.from_mapping(
        {
            "KAFKA_ENABLED": "true",
            "KAFKA_BOOTSTRAP_SERVERS": bootstrap_servers,
            "KAFKA_CLAIM_SUBMITTED_TOPIC": topic,
            "KAFKA_CONSUMER_GROUP_ID": f"claims-integration-{identity}",
            "KAFKA_DELIVERY_TIMEOUT_MS": "10000",
            "KAFKA_CONSUMER_POLL_SECONDS": "0.5",
        }
    )
    try:
        yield settings
    finally:
        delete_future = admin.delete_topics([topic], operation_timeout=10)[topic]
        delete_future.result(timeout=15)


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
    return json.dumps(
        claim.canonical_document(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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

    first = postgres_repository.get_duplicate_check_for_claim(7)
    second = postgres_repository.get_duplicate_check_for_claim(8)
    assert first is not None
    assert second is not None
    assert [(match.claim_id, match.insurer_id) for match in first.matches] == [
        (8, "harbour-shield")
    ]
    assert [(match.claim_id, match.insurer_id) for match in second.matches] == [
        (7, "northstar-mutual")
    ]
