from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from web3 import Web3

from backend.app.blockchain import ChainAssessment, ChainClaim
from duplicates import DuplicateCheck
from integrations.kafka import ClaimSubmittedEvent
from integrations.kafka.scoring_worker import ClaimScoringHandler
from integrations.postgres import AssessmentRecord
from model.contracts import FraudReason, FraudScore


def claim_payload() -> bytes:
    return json.dumps(
        {
            "schemaVersion": 3,
            "insurerId": "northstar-mutual",
            "claimReference": "synthetic-worker-1",
            "policyReference": "synthetic-policy-42",
            "claimType": "collision",
            "incidentDate": "2026-07-13",
            "claimAmountUsd": 2500,
            "policyPremiumUsd": 480,
            "vehicleAge": 6,
            "vehicleType": "sedan",
            "country": "Nigeria",
            "regionType": "urban",
            "thirdPartyInjuryFlag": False,
            "totalLossFlag": False,
            "description": "Synthetic bumper damage for worker testing",
            "evidence": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def claim_event(payload: bytes | None = None) -> ClaimSubmittedEvent:
    value = payload or claim_payload()
    return ClaimSubmittedEvent.create(
        chain_id=11_155_111,
        contract_address="0x1111111111111111111111111111111111111111",
        claim_id=7,
        claimant="0x2222222222222222222222222222222222222222",
        claim_hash=Web3.keccak(value).hex(),
        data_pointer="ipfs://bafy-test",
        block_number=100,
        block_hash="0xblock",
        transaction_hash="0xtransaction",
        log_index=2,
        event_timestamp=1_750_000_000,
    )


class FakeIPFS:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.downloads = 0

    def download_pointer(self, pointer, *, attempts=3):
        assert pointer == "ipfs://bafy-test"
        self.downloads += 1
        return self.payload


class FakeScorer:
    def __init__(self):
        self.calls = 0

    def score(self, claim):
        assert claim.schema_version == 3
        assert claim.vehicle_age == 6
        self.calls += 1
        return FraudScore(
            probability=0.68,
            score_basis_points=6800,
            threshold=0.47,
            flagged=True,
            model_version="african-motor-xgboost-v1",
            reasons=(
                FraudReason("claim_amount_usd", "Claim amount", 0.42),
            ),
        )


class FakeDuplicateDetector:
    def __init__(self):
        self.calls = []

    def check(self, event, claim):
        assert claim.insurer_id == "northstar-mutual"
        self.calls.append((event.claim_id, claim.insurer_id))
        return DuplicateCheck(
            insurer_id=claim.insurer_id,
            fingerprint_version="incident-hmac-sha256-v1",
        )


class FakeFeatureProcessor:
    def __init__(self):
        self.calls = []

    def process(self, event, claim, duplicate_check):
        self.calls.append(
            (
                event.claim_id,
                claim.insurer_id,
                len(duplicate_check.matches),
            )
        )
        return SimpleNamespace(feature_version="claim-processing-v1")


class FakeRepository:
    def __init__(self, record: AssessmentRecord | None = None):
        self.record = record
        self.completed = None
        self.failed = None

    def get_by_event_id(self, event_id):
        if self.record and self.record.event_id == event_id:
            return self.record
        return None

    def save_scored(self, record):
        self.record = record

    def mark_completed(self, event_id, *, transaction_hash, block_number):
        self.completed = (event_id, transaction_hash, block_number)
        self.record = AssessmentRecord(
            **{
                **self.record.__dict__,
                "processing_status": "completed",
                "transaction_hash": transaction_hash,
                "block_number": block_number,
                "error": None,
            }
        )

    def mark_failed(self, event_id, error):
        self.failed = (event_id, error)


class FakeRegistry:
    def __init__(self, *, status=0, fraud_score=0):
        self.status = status
        self.fraud_score = fraud_score
        self.assessments = []

    def get_claim(self, claim_id):
        return ChainClaim(
            claim_id=claim_id,
            claimant="0x2222222222222222222222222222222222222222",
            claim_hash="0xhash",
            data_pointer="ipfs://bafy-test",
            status=self.status,
            fraud_score=self.fraud_score,
            submitted_at=100,
            updated_at=100,
        )

    def assess_claim(self, claim_id, status, fraud_score):
        self.assessments.append((claim_id, status, fraud_score))
        self.status = status
        self.fraud_score = fraud_score
        return ChainAssessment(
            transaction_hash="0xassessment",
            block_number=101,
            status=status,
            fraud_score=fraud_score,
        )


def test_worker_scores_persists_and_assesses_one_verified_claim():
    payload = claim_payload()
    event = claim_event(payload)
    repository = FakeRepository()
    scorer = FakeScorer()
    duplicate_detector = FakeDuplicateDetector()
    feature_processor = FakeFeatureProcessor()
    registry = FakeRegistry()
    handler = ClaimScoringHandler(
        ipfs=FakeIPFS(payload),
        scorer=scorer,
        duplicate_detector=duplicate_detector,
        feature_processor=feature_processor,
        repository=repository,
        registry=registry,
    )

    handler(event)

    assert scorer.calls == 1
    assert duplicate_detector.calls == [(7, "northstar-mutual")]
    assert feature_processor.calls == [(7, "northstar-mutual", 0)]
    assert repository.record.processing_status == "completed"
    assert registry.assessments == [(7, 4, 6800)]
    assert repository.completed == (event.event_id, "0xassessment", 101)


def test_worker_commits_a_duplicate_without_scoring_again():
    event = claim_event()
    record = AssessmentRecord(
        event_id=event.event_id,
        chain_id=event.chain_id,
        contract_address=event.contract_address,
        claim_id=event.claim_id,
        model_version="african-motor-xgboost-v1",
        probability=0.68,
        threshold=0.47,
        fraud_score=6800,
        status="Flagged",
        reasons=(),
        processing_status="completed",
    )
    ipfs = FakeIPFS(claim_payload())
    scorer = FakeScorer()
    feature_processor = FakeFeatureProcessor()
    handler = ClaimScoringHandler(
        ipfs=ipfs,
        scorer=scorer,
        duplicate_detector=FakeDuplicateDetector(),
        feature_processor=feature_processor,
        repository=FakeRepository(record),
        registry=FakeRegistry(status=4, fraud_score=6800),
    )

    handler(event)

    assert ipfs.downloads == 0
    assert scorer.calls == 0
    assert feature_processor.calls == []


def test_worker_recovers_when_chain_write_finished_before_database_update():
    payload = claim_payload()
    event = claim_event(payload)
    record = AssessmentRecord(
        event_id=event.event_id,
        chain_id=event.chain_id,
        contract_address=event.contract_address,
        claim_id=event.claim_id,
        model_version="african-motor-xgboost-v1",
        probability=0.68,
        threshold=0.47,
        fraud_score=6800,
        status="Flagged",
        reasons=(),
        processing_status="failed",
    )
    repository = FakeRepository(record)
    registry = FakeRegistry(status=4, fraud_score=6800)
    handler = ClaimScoringHandler(
        ipfs=FakeIPFS(payload),
        scorer=FakeScorer(),
        duplicate_detector=FakeDuplicateDetector(),
        feature_processor=FakeFeatureProcessor(),
        repository=repository,
        registry=registry,
    )

    handler(event)

    assert registry.assessments == []
    assert repository.record.processing_status == "completed"


def test_worker_rejects_changed_ipfs_bytes_before_scoring():
    payload = claim_payload()
    scorer = FakeScorer()
    feature_processor = FakeFeatureProcessor()
    repository = FakeRepository()
    handler = ClaimScoringHandler(
        ipfs=FakeIPFS(b"changed"),
        scorer=scorer,
        duplicate_detector=FakeDuplicateDetector(),
        feature_processor=feature_processor,
        repository=repository,
        registry=FakeRegistry(),
    )

    with pytest.raises(ValueError, match="hash"):
        handler(claim_event(payload))

    assert scorer.calls == 0
    assert feature_processor.calls == []
    assert repository.record is None
