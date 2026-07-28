from dataclasses import asdict
from datetime import UTC, date, datetime

import pytest

from backend.app.models import ClaimSubmission
from duplicates import DuplicateCheck, DuplicateMatch
from integrations.kafka import ClaimSubmittedEvent
from integrations.postgres import (
    FEATURE_VERSION,
    POLICY_FINGERPRINT_VERSION,
    ClaimFeatureConfigurationError,
    ClaimFeatureProcessingError,
    ClaimFeatureProcessor,
    ClaimFeatureSnapshot,
)


class FakeFeatureStore:
    def __init__(self) -> None:
        self.inputs = []

    def record_feature_snapshot(self, values):
        self.inputs.append(values)
        return ClaimFeatureSnapshot(
            **asdict(values),
            prior_policy_claim_count=2,
            prior_insurer_claim_count=4,
            prior_insurer_average_claim_amount_usd=2000.0,
            claim_to_prior_insurer_average_ratio=1.25,
        )


def claim(
    *,
    insurer_id: str = "northstar-mutual",
    policy_reference: str = " Synthetic Policy 42 ",
    incident_date: date = date(2026, 7, 13),
) -> ClaimSubmission:
    return ClaimSubmission(
        insurerId=insurer_id,
        claimReference="synthetic-claim-1",
        policyReference=policy_reference,
        claimType="collision",
        incidentDate=incident_date,
        claimAmountUsd=2500,
        policyPremiumUsd=500,
        vehicleAge=6,
        vehicleType="sedan",
        country="Nigeria",
        regionType="urban",
        thirdPartyInjuryFlag=False,
        totalLossFlag=False,
        description="Synthetic claim for feature processing",
        evidence=[],
    )


def event(*, claim_id: int = 7) -> ClaimSubmittedEvent:
    event_timestamp = int(datetime(2026, 7, 20, tzinfo=UTC).timestamp())
    return ClaimSubmittedEvent.create(
        chain_id=11_155_111,
        contract_address="0xABCDEF",
        claim_id=claim_id,
        claimant="0xclaimant",
        claim_hash="0xhash",
        data_pointer=f"ipfs://claim-{claim_id}",
        block_number=100 + claim_id,
        block_hash=f"0xblock-{claim_id}",
        transaction_hash=f"0xtransaction-{claim_id}",
        log_index=0,
        event_timestamp=event_timestamp,
    )


def duplicate_check(
    insurer_id: str = "northstar-mutual",
) -> DuplicateCheck:
    return DuplicateCheck(
        insurer_id=insurer_id,
        fingerprint_version="incident-hmac-sha256-v1",
        matches=(DuplicateMatch(3, "harbour-shield"),),
    )


def test_processor_builds_and_persists_privacy_safe_features():
    store = FakeFeatureStore()
    processor = ClaimFeatureProcessor(b"feature-test-key-" * 3, store)

    snapshot = processor.process(event(), claim(), duplicate_check())

    assert snapshot.feature_version == FEATURE_VERSION
    assert snapshot.policy_fingerprint_version == POLICY_FINGERPRINT_VERSION
    assert snapshot.contract_address == "0xabcdef"
    assert snapshot.report_delay_days == 7
    assert snapshot.claim_to_premium_ratio == 5.0
    assert snapshot.cross_insurer_duplicate_match_count == 1
    assert snapshot.prior_policy_claim_count == 2
    assert len(snapshot.policy_reference_fingerprint) == 64
    assert "policy" not in snapshot.policy_reference_fingerprint
    assert not hasattr(snapshot, "description")
    assert not hasattr(snapshot, "claim_reference")
    assert not hasattr(snapshot, "policy_reference")


def test_policy_fingerprint_is_normalized_and_scoped_to_the_insurer():
    store = FakeFeatureStore()
    processor = ClaimFeatureProcessor(b"feature-test-key-" * 3, store)

    processor.process(
        event(claim_id=7),
        claim(policy_reference=" Synthetic   Policy 42 "),
        duplicate_check(),
    )
    processor.process(
        event(claim_id=8),
        claim(policy_reference="synthetic policy 42"),
        duplicate_check(),
    )
    processor.process(
        event(claim_id=9),
        claim(
            insurer_id="harbour-shield",
            policy_reference="synthetic policy 42",
        ),
        duplicate_check("harbour-shield"),
    )

    fingerprints = [
        values.policy_reference_fingerprint for values in store.inputs
    ]
    assert fingerprints[0] == fingerprints[1]
    assert fingerprints[2] != fingerprints[0]


def test_processor_rejects_an_incident_after_the_block_event():
    store = FakeFeatureStore()
    processor = ClaimFeatureProcessor(b"feature-test-key-" * 3, store)

    with pytest.raises(ClaimFeatureProcessingError, match="later"):
        processor.process(
            event(),
            claim(incident_date=date(2026, 7, 21)),
            duplicate_check(),
        )

    assert store.inputs == []


def test_processor_rejects_a_duplicate_result_for_another_insurer():
    processor = ClaimFeatureProcessor(b"feature-test-key-" * 3, FakeFeatureStore())

    with pytest.raises(ClaimFeatureProcessingError, match="insurer"):
        processor.process(
            event(),
            claim(),
            duplicate_check("harbour-shield"),
        )


def test_processor_requires_a_long_fingerprint_key():
    with pytest.raises(ClaimFeatureConfigurationError, match="32 bytes"):
        ClaimFeatureProcessor(b"too-short", FakeFeatureStore())


def test_processor_reads_the_existing_worker_secret(monkeypatch):
    monkeypatch.setenv("DUPLICATE_FINGERPRINT_KEY", "x" * 32)

    processor = ClaimFeatureProcessor.from_env(FakeFeatureStore())

    assert isinstance(processor, ClaimFeatureProcessor)


def test_processor_requires_the_worker_secret(monkeypatch):
    monkeypatch.delenv("DUPLICATE_FINGERPRINT_KEY", raising=False)

    with pytest.raises(ClaimFeatureConfigurationError, match="required"):
        ClaimFeatureProcessor.from_env(FakeFeatureStore())
