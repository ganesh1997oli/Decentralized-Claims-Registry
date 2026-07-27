"""PostgreSQL integration tests for cross-insurer duplicate screening."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest

from backend.app.models import ClaimSubmission
from duplicates import CrossInsurerDuplicateDetector
from integrations.kafka import ClaimSubmittedEvent


pytestmark = pytest.mark.integration


def claim(insurer_id: str) -> ClaimSubmission:
    return ClaimSubmission(
        insurerId=insurer_id,
        claimReference=f"{insurer_id}-claim",
        policyReference=f"{insurer_id}-policy",
        claimType="collision",
        incidentDate=date(2026, 7, 13),
        claimAmountUsd=2500,
        policyPremiumUsd=480,
        vehicleAge=6,
        vehicleType="sedan",
        country="Nigeria",
        regionType="urban",
        thirdPartyInjuryFlag=False,
        totalLossFlag=False,
        description=f"Synthetic incident reported to {insurer_id}",
        evidence=[],
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


def test_concurrent_other_insurer_submissions_become_mutual_matches(
    postgres_repository,
):
    detector = CrossInsurerDuplicateDetector(
        b"integration-key-" * 3,
        postgres_repository,
    )
    barrier = Barrier(2)

    def screen(claim_id: int, insurer_id: str):
        barrier.wait(timeout=5)
        return detector.check(event(claim_id), claim(insurer_id))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(screen, 7, "northstar-mutual")
        second_future = executor.submit(screen, 8, "harbour-shield")
        initial_results = (first_future.result(), second_future.result())

    # The advisory lock serializes equal fingerprints: the later transaction
    # observes the earlier one instead of both returning a false negative.
    assert sorted(len(result.matches) for result in initial_results) == [0, 1]

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
