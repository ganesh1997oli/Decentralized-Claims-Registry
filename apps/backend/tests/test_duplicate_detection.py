from dataclasses import replace

import pytest

from apps.backend.app.models import ClaimSubmission
from packages.duplicates import (
    CrossInsurerDuplicateDetector,
    DuplicateCheck,
    DuplicateDetectionConfigurationError,
    DuplicateMatch,
)
from packages.integrations.kafka import ClaimSubmittedEvent


class InMemoryDuplicateStore:
    """Behavioral test adapter for the detector's persistence seam."""

    def __init__(self):
        self.rows = {}

    def record_and_find_duplicates(self, **values):
        key = (
            values["chain_id"],
            values["contract_address"].lower(),
            values["claim_id"],
        )
        self.rows[key] = dict(values)
        matches = tuple(
            DuplicateMatch(claim_id=row["claim_id"], insurer_id=row["insurer_id"])
            for row in sorted(self.rows.values(), key=lambda item: item["claim_id"])
            if row["chain_id"] == values["chain_id"]
            and row["contract_address"].lower()
            == values["contract_address"].lower()
            and row["fingerprint_version"] == values["fingerprint_version"]
            and row["incident_fingerprint"] == values["incident_fingerprint"]
            and row["claim_id"] != values["claim_id"]
            and row["insurer_id"] != values["insurer_id"]
        )
        return DuplicateCheck(
            insurer_id=values["insurer_id"],
            fingerprint_version=values["fingerprint_version"],
            matches=matches,
        )


def claim(
    *,
    insurer_id: str,
    claim_reference: str,
    policy_reference: str,
    description: str,
    claim_amount_usd: float = 2500,
) -> ClaimSubmission:
    return ClaimSubmission.model_validate(
        {
            "insurerId": insurer_id,
            "claimReference": claim_reference,
            "policyReference": policy_reference,
            "claimType": "collision",
            "incidentDate": "2026-07-13",
            "claimAmountUsd": claim_amount_usd,
            "policyPremiumUsd": 480,
            "vehicleAge": 6,
            "vehicleType": "sedan",
            "country": "Nigeria",
            "regionType": "urban",
            "thirdPartyInjuryFlag": False,
            "totalLossFlag": False,
            "description": description,
            "evidence": [],
        }
    )


def event(claim_id: int) -> ClaimSubmittedEvent:
    return ClaimSubmittedEvent.create(
        chain_id=11_155_111,
        contract_address="0x1111111111111111111111111111111111111111",
        claim_id=claim_id,
        claimant="0x2222222222222222222222222222222222222222",
        claim_hash=f"0xhash-{claim_id}",
        data_pointer=f"ipfs://claim-{claim_id}",
        block_number=100 + claim_id,
        block_hash=f"0xblock-{claim_id}",
        transaction_hash=f"0xtransaction-{claim_id}",
        log_index=0,
        event_timestamp=1_750_000_000 + claim_id,
    )


def detector(store: InMemoryDuplicateStore) -> CrossInsurerDuplicateDetector:
    return CrossInsurerDuplicateDetector(b"test-key-" * 4, store)


def test_detector_matches_same_incident_from_another_insurer():
    store = InMemoryDuplicateStore()
    duplicate_detector = detector(store)
    first = claim(
        insurer_id="northstar-mutual",
        claim_reference="northstar-100",
        policy_reference="northstar-policy",
        description="Synthetic bumper damage reported to Northstar",
    )
    second = claim(
        insurer_id="harbour-shield",
        claim_reference="harbour-900",
        policy_reference="harbour-policy",
        description="The same synthetic incident described differently",
    )

    first_result = duplicate_detector.check(event(7), first)
    second_result = duplicate_detector.check(event(8), second)

    assert first_result.duplicate_detected is False
    assert second_result.duplicate_detected is True
    assert second_result.matches == (DuplicateMatch(7, "northstar-mutual"),)


def test_detector_ignores_repeated_submission_from_the_same_insurer():
    store = InMemoryDuplicateStore()
    duplicate_detector = detector(store)
    first = claim(
        insurer_id="northstar-mutual",
        claim_reference="northstar-100",
        policy_reference="policy-one",
        description="First submission",
    )
    second = claim(
        insurer_id="northstar-mutual",
        claim_reference="northstar-101",
        policy_reference="policy-two",
        description="Second submission",
    )

    duplicate_detector.check(event(7), first)
    result = duplicate_detector.check(event(8), second)

    assert result.duplicate_detected is False


def test_detector_does_not_match_a_materially_different_incident():
    store = InMemoryDuplicateStore()
    duplicate_detector = detector(store)
    first = claim(
        insurer_id="northstar-mutual",
        claim_reference="northstar-100",
        policy_reference="policy-one",
        description="First incident",
    )
    second = claim(
        insurer_id="harbour-shield",
        claim_reference="harbour-900",
        policy_reference="policy-two",
        description="Different incident",
        claim_amount_usd=2600,
    )

    duplicate_detector.check(event(7), first)
    result = duplicate_detector.check(event(8), second)

    assert result.duplicate_detected is False


def test_detector_requires_a_long_private_key(monkeypatch):
    store = InMemoryDuplicateStore()

    with pytest.raises(
        DuplicateDetectionConfigurationError,
        match="at least 32 bytes",
    ):
        CrossInsurerDuplicateDetector(b"too-short", store)

    monkeypatch.delenv("DUPLICATE_FINGERPRINT_KEY", raising=False)
    with pytest.raises(
        DuplicateDetectionConfigurationError,
        match="DUPLICATE_FINGERPRINT_KEY",
    ):
        CrossInsurerDuplicateDetector.from_env(store)

    monkeypatch.setenv("DUPLICATE_FINGERPRINT_KEY", "x" * 32)
    configured = CrossInsurerDuplicateDetector.from_env(store)
    assert configured.check(
        event(7),
        claim(
            insurer_id="northstar-mutual",
            claim_reference="configured-claim",
            policy_reference="configured-policy",
            description="Configured detector",
        ),
    ).duplicate_detected is False


def test_fingerprint_compatibility_vector_changes_only_with_a_version_bump():
    store = InMemoryDuplicateStore()
    duplicate_detector = CrossInsurerDuplicateDetector(
        b"0123456789abcdef0123456789abcdef",
        store,
    )

    duplicate_detector.check(
        event(7),
        claim(
            insurer_id="northstar-mutual",
            claim_reference="reference-not-fingerprinted",
            policy_reference="policy-not-fingerprinted",
            description="Description is not fingerprinted",
        ),
    )

    stored = next(iter(store.rows.values()))
    assert stored["incident_fingerprint"] == (
        "d0d0f502f2413f0359a53381fdb6c04b5da29fbae8258c71ed66a5b42c9b99ef"
    )


def test_detector_scopes_matches_to_one_chain_and_contract():
    store = InMemoryDuplicateStore()
    duplicate_detector = detector(store)
    first_claim = claim(
        insurer_id="northstar-mutual",
        claim_reference="northstar-100",
        policy_reference="northstar-policy",
        description="First deployment",
    )
    second_claim = claim(
        insurer_id="harbour-shield",
        claim_reference="harbour-900",
        policy_reference="harbour-policy",
        description="Another deployment",
    )

    duplicate_detector.check(event(7), first_claim)
    another_chain = duplicate_detector.check(
        replace(event(8), chain_id=1),
        second_claim,
    )
    another_contract = duplicate_detector.check(
        replace(
            event(9),
            contract_address="0x9999999999999999999999999999999999999999",
        ),
        second_claim,
    )

    assert another_chain.duplicate_detected is False
    assert another_contract.duplicate_detected is False


def test_detector_returns_every_other_insurer_match_in_claim_order():
    store = InMemoryDuplicateStore()
    duplicate_detector = detector(store)
    for claim_id, insurer_id in (
        (9, "cedar-insurance"),
        (7, "northstar-mutual"),
    ):
        duplicate_detector.check(
            event(claim_id),
            claim(
                insurer_id=insurer_id,
                claim_reference=f"{insurer_id}-claim",
                policy_reference=f"{insurer_id}-policy",
                description=f"Description from {insurer_id}",
            ),
        )

    result = duplicate_detector.check(
        event(10),
        claim(
            insurer_id="harbour-shield",
            claim_reference="harbour-claim",
            policy_reference="harbour-policy",
            description="Description from Harbour Shield",
        ),
    )

    assert result.matches == (
        DuplicateMatch(7, "northstar-mutual"),
        DuplicateMatch(9, "cedar-insurance"),
    )
