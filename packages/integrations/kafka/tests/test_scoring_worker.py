from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from prometheus_client import generate_latest
from web3 import Web3

from apps.backend.app.blockchain import ChainAssessment, ChainClaim
from apps.backend.app.models import ClaimSubmission, StoredClaimDocument
from apps.backend.app.submission_auth import (
    ClaimAuthorizationSigner,
    InsurerPrincipal,
)
from packages.duplicates import DuplicateCheck
from packages.integrations.kafka import (
    ClaimSubmittedEvent,
    KafkaClaimEventConsumer,
    KafkaSettings,
)
from packages.integrations.kafka.scoring_worker import (
    ClaimScoringHandler,
    JsonlClaimDeadLetterSink,
    MonitoredClaimHandler,
    MonitoredScorer,
    PermanentClaimProcessingError,
    QuarantiningClaimHandler,
    scoring_dead_letter_path,
)
from packages.integrations.postgres import AssessmentRecord
from packages.model.contracts import FraudReason, FraudScore
from packages.observability import ScoringMetrics

AUTHORIZATION = ClaimAuthorizationSigner(
    b"worker-test-claim-authorization-key-32-bytes"
)
PRINCIPAL = InsurerPrincipal(
    insurer_id="northstar-mutual",
    credential_id="northstar-test-v1",
    signer_address="0x1111111111111111111111111111111111111111",
    permitted_operations=frozenset({"submit_claim"}),
    daily_quota=25,
)


def claim_payload() -> bytes:
    claim = ClaimSubmission.model_validate(
        {
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
        }
    )
    return AUTHORIZATION.authorized_claim_bytes(claim, PRINCIPAL)


def claim_event(payload: bytes | None = None) -> ClaimSubmittedEvent:
    value = payload or claim_payload()
    return ClaimSubmittedEvent.create(
        chain_id=11_155_111,
        contract_address="0x1111111111111111111111111111111111111111",
        claim_id=7,
        claimant=PRINCIPAL.signer_address,
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


class PointerIPFS:
    """Return different immutable IPFS bytes for consecutive claim events."""

    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads

    def download_pointer(self, pointer, *, attempts=3):
        return self.payloads[pointer]


class FakeDeadLetter:
    """Capture quarantined metadata without writing a real operations file."""

    def __init__(self):
        self.entries = []

    def record(self, event, error):
        self.entries.append((event, error))


class FakeKafkaMessage:
    """Small Confluent-Kafka message substitute used by the offset regression."""

    def __init__(self, value: bytes):
        self._value = value

    def value(self):
        return self._value

    def error(self):
        return None


class QueueKafkaConsumer:
    """Deliver messages in one fixed order, representing a single partition."""

    def __init__(self, messages: list[FakeKafkaMessage]):
        self.messages = iter(messages)
        self.commits = []

    def subscribe(self, _topics):
        return None

    def poll(self, _timeout):
        return next(self.messages, None)

    def commit(self, *, message, asynchronous):
        self.commits.append((message, asynchronous))

    def close(self):
        return None


class FakeScorer:
    def __init__(self):
        self.calls = 0

    def score(self, claim):
        assert claim.schema_version == 5
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
            claimant=PRINCIPAL.signer_address,
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
        authorization=AUTHORIZATION,
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
        authorization=AUTHORIZATION,
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
        authorization=AUTHORIZATION,
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
        authorization=AUTHORIZATION,
    )

    with pytest.raises(ValueError, match="hash"):
        handler(claim_event(payload))

    assert scorer.calls == 0
    assert feature_processor.calls == []
    assert repository.record is None


def test_worker_rejects_claim_not_attested_by_authenticated_gateway():
    attacker = ClaimAuthorizationSigner(
        b"different-worker-authorization-key-32-bytes"
    )
    unsigned_claim = ClaimSubmission.model_validate(
        {
            "insurerId": "northstar-mutual",
            "claimReference": "synthetic-worker-forged",
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
            "description": "Forged insurer identity",
            "evidence": [],
        }
    )
    payload = attacker.authorized_claim_bytes(unsigned_claim, PRINCIPAL)
    scorer = FakeScorer()
    repository = FakeRepository()
    handler = ClaimScoringHandler(
        ipfs=FakeIPFS(payload),
        scorer=scorer,
        duplicate_detector=FakeDuplicateDetector(),
        feature_processor=FakeFeatureProcessor(),
        repository=repository,
        registry=FakeRegistry(),
        authorization=AUTHORIZATION,
    )

    with pytest.raises(PermanentClaimProcessingError) as raised:
        handler(claim_event(payload))

    assert raised.value.reason_code == "invalid_claim_authorization"
    assert scorer.calls == 0
    assert repository.record is None


def test_worker_rejects_attested_claim_submitted_by_a_different_wallet():
    payload = claim_payload()
    event = replace(
        claim_event(payload),
        claimant="0x2222222222222222222222222222222222222222",
    )
    scorer = FakeScorer()
    repository = FakeRepository()
    handler = ClaimScoringHandler(
        ipfs=FakeIPFS(payload),
        scorer=scorer,
        duplicate_detector=FakeDuplicateDetector(),
        feature_processor=FakeFeatureProcessor(),
        repository=repository,
        registry=FakeRegistry(),
        authorization=AUTHORIZATION,
    )

    with pytest.raises(PermanentClaimProcessingError) as raised:
        handler(event)

    assert raised.value.reason_code == "claimant_identity_mismatch"
    assert scorer.calls == 0
    assert repository.record is None


def test_malformed_claim_is_quarantined_before_the_next_claim_is_processed():
    """A permanent poison claim must not hold the partition offset forever."""

    malformed_payload = b'{"schemaVersion":5,"claimReference":"incomplete"}'
    valid_payload = claim_payload()
    malformed_event = replace(
        claim_event(malformed_payload),
        event_id=ClaimSubmittedEvent.make_event_id(
            11_155_111,
            "0xmalformed",
            2,
        ),
        data_pointer="ipfs://malformed-claim",
        transaction_hash="0xmalformed",
    )
    valid_event = replace(
        claim_event(valid_payload),
        event_id=ClaimSubmittedEvent.make_event_id(11_155_111, "0xvalid", 3),
        claim_id=8,
        data_pointer="ipfs://valid-claim",
        transaction_hash="0xvalid",
        log_index=3,
    )
    repository = FakeRepository()
    scorer = FakeScorer()
    dead_letter = FakeDeadLetter()
    handler = ClaimScoringHandler(
        ipfs=PointerIPFS(
            {
                malformed_event.data_pointer: malformed_payload,
                valid_event.data_pointer: valid_payload,
            }
        ),
        scorer=scorer,
        duplicate_detector=FakeDuplicateDetector(),
        feature_processor=FakeFeatureProcessor(),
        repository=repository,
        registry=FakeRegistry(),
        authorization=AUTHORIZATION,
    )
    partition_safe_handler = QuarantiningClaimHandler(
        handler,
        dead_letter=dead_letter,
    )
    messages = [
        FakeKafkaMessage(malformed_event.to_json_bytes()),
        FakeKafkaMessage(valid_event.to_json_bytes()),
    ]
    fake_kafka = QueueKafkaConsumer(messages)
    consumer = KafkaClaimEventConsumer(KafkaSettings(), consumer=fake_kafka)

    # Both events represent the order in one Kafka partition. The first call
    # must return normally after storing the quarantine; that return lets
    # Kafka commit its offset and deliver the second event.
    assert consumer.process_next(partition_safe_handler)
    assert consumer.process_next(partition_safe_handler)

    assert len(dead_letter.entries) == 1
    rejected_event, error = dead_letter.entries[0]
    assert rejected_event == malformed_event
    assert isinstance(error, PermanentClaimProcessingError)
    assert error.reason_code == "invalid_claim_schema"
    assert scorer.calls == 1
    assert repository.completed == (valid_event.event_id, "0xassessment", 101)
    assert fake_kafka.commits == [
        (messages[0], False),
        (messages[1], False),
    ]


def test_quarantine_wrapper_never_skips_a_transient_failure():
    """Only immutable input defects are safe to commit past."""

    dead_letter = FakeDeadLetter()

    def unavailable_dependency(_event):
        raise RuntimeError("IPFS gateway temporarily unavailable")

    partition_safe_handler = QuarantiningClaimHandler(
        unavailable_dependency,
        dead_letter=dead_letter,
    )

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        partition_safe_handler(claim_event())

    assert dead_letter.entries == []


def test_quarantine_wrapper_fails_closed_when_dead_letter_storage_fails():
    """A failed audit write must leave Kafka free to replay the claim."""

    class UnwritableDeadLetter:
        def record(self, _event, _error):
            raise OSError("dead-letter volume is unavailable")

    def permanently_invalid(_event):
        raise PermanentClaimProcessingError(
            "invalid_claim_schema",
            "Claim document does not match the supported stored-claim schema",
        )

    partition_safe_handler = QuarantiningClaimHandler(
        permanently_invalid,
        dead_letter=UnwritableDeadLetter(),
    )
    message = FakeKafkaMessage(claim_event().to_json_bytes())
    fake_kafka = QueueKafkaConsumer([message])
    consumer = KafkaClaimEventConsumer(KafkaSettings(), consumer=fake_kafka)

    with pytest.raises(OSError, match="dead-letter volume"):
        consumer.process_next(partition_safe_handler)

    # The commit call occurs after the handler. Propagating the storage failure
    # therefore guarantees that Kafka will redeliver instead of losing the only
    # evidence that this immutable claim was rejected.
    assert fake_kafka.commits == []


def test_jsonl_dead_letter_contains_public_provenance_not_claim_bytes(tmp_path):
    """Operators need replay coordinates, not a second copy of claim data."""

    path = tmp_path / "worker-state" / "dead-letter.jsonl"
    event = claim_event()
    error = PermanentClaimProcessingError(
        "invalid_claim_schema",
        "Claim document does not match the supported stored-claim schema",
    )

    JsonlClaimDeadLetterSink(path).record(event, error)

    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["reasonCode"] == "invalid_claim_schema"
    assert entry["eventId"] == event.event_id
    assert entry["claimId"] == event.claim_id
    assert entry["transactionHash"] == event.transaction_hash
    assert entry["dataPointer"] == event.data_pointer
    assert "submissionAuthorization" not in entry
    assert "description" not in entry


def test_default_dead_letter_path_is_scoped_to_the_deployment(tmp_path):
    path = scoring_dead_letter_path(
        {"SCORING_STATE_DIR": str(tmp_path)},
        deployment_id="sepolia-gasless-v2",
    )

    assert path == tmp_path / "sepolia-gasless-v2-dead-letter.jsonl"


def test_monitored_scorer_records_only_model_work(monkeypatch):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    metrics = ScoringMetrics.start_from_env()
    # Two deterministic clock values make the test independent of machine speed.
    clock_values = iter((10.0, 10.25))
    scorer = MonitoredScorer(
        FakeScorer(),
        metrics,
        clock=lambda: next(clock_values),
    )

    score = scorer.score(
        # Use the real schema parser so the wrapper is tested with the object it
        # receives in the running worker.
        StoredClaimDocument.model_validate_json(claim_payload())
    )

    output = generate_latest(metrics.registry).decode("utf-8")
    assert score.probability == 0.68
    assert "claims_scoring_model_inference_seconds_sum 0.25" in output
    assert "claims_scoring_last_probability 0.68" in output


def test_monitored_handler_records_success_and_failure(monkeypatch):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    metrics = ScoringMetrics.start_from_env()
    successful_clock = iter((20.0, 20.5))
    seen = []
    handler = MonitoredClaimHandler(
        lambda event: seen.append(event.event_id),
        metrics,
        clock=lambda: next(successful_clock),
    )

    event = claim_event()
    handler(event)

    failing_clock = iter((30.0, 31.0))

    def fail(_event):
        raise RuntimeError("temporary dependency failure")

    failing_handler = MonitoredClaimHandler(
        fail,
        metrics,
        clock=lambda: next(failing_clock),
    )
    with pytest.raises(RuntimeError, match="temporary dependency"):
        failing_handler(event)

    quarantined_clock = iter((40.0, 40.2))

    def reject_permanently(_event):
        raise PermanentClaimProcessingError(
            "invalid_claim_schema",
            "Claim document does not match the supported stored-claim schema",
        )

    quarantined_handler = QuarantiningClaimHandler(
        reject_permanently,
        dead_letter=FakeDeadLetter(),
        metrics=metrics,
        clock=lambda: next(quarantined_clock),
    )
    quarantined_handler(event)

    output = generate_latest(metrics.registry).decode("utf-8")
    assert seen == [event.event_id]
    assert 'claims_scoring_events_total{outcome="completed"} 1.0' in output
    assert 'claims_scoring_events_total{outcome="failed"} 1.0' in output
    assert 'claims_scoring_events_total{outcome="quarantined"} 1.0' in output
