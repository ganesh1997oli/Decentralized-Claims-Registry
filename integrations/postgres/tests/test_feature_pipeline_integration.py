"""PostgreSQL integration tests for versioned claim feature snapshots."""

from datetime import UTC, date, datetime

import pytest

from backend.app.models import ClaimSubmission
from duplicates import DuplicateCheck, DuplicateMatch
from integrations.kafka import ClaimSubmittedEvent
from integrations.postgres import ClaimFeatureProcessor


pytestmark = pytest.mark.integration


def claim(
    *,
    claim_id: int,
    policy_reference: str,
    claim_amount_usd: float,
) -> ClaimSubmission:
    return ClaimSubmission(
        insurerId="northstar-mutual",
        claimReference=f"northstar-claim-{claim_id}",
        policyReference=policy_reference,
        claimType="collision",
        incidentDate=date(2026, 7, 13),
        claimAmountUsd=claim_amount_usd,
        policyPremiumUsd=500,
        vehicleAge=6,
        vehicleType="sedan",
        country="Nigeria",
        regionType="urban",
        thirdPartyInjuryFlag=False,
        totalLossFlag=False,
        description="Synthetic feature-pipeline integration claim",
        evidence=[],
    )


def event(claim_id: int) -> ClaimSubmittedEvent:
    event_timestamp = int(datetime(2026, 7, 20, tzinfo=UTC).timestamp())
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
        event_timestamp=event_timestamp + claim_id,
    )


def duplicate_check(*, with_match: bool = False) -> DuplicateCheck:
    matches = (
        (DuplicateMatch(99, "harbour-shield"),)
        if with_match
        else ()
    )
    return DuplicateCheck(
        insurer_id="northstar-mutual",
        fingerprint_version="incident-hmac-sha256-v1",
        matches=matches,
    )


def test_snapshots_keep_historical_counts_averages_and_replay_values(
    postgres_repository,
):
    processor = ClaimFeatureProcessor(
        b"feature-integration-key-" * 2,
        postgres_repository,
    )

    first = processor.process(
        event(7),
        claim(
            claim_id=7,
            policy_reference="policy-a",
            claim_amount_usd=1000,
        ),
        duplicate_check(),
    )
    second = processor.process(
        event(8),
        claim(
            claim_id=8,
            policy_reference="policy-a",
            claim_amount_usd=3000,
        ),
        duplicate_check(),
    )
    third = processor.process(
        event(9),
        claim(
            claim_id=9,
            policy_reference="policy-b",
            claim_amount_usd=2000,
        ),
        duplicate_check(),
    )

    assert first.prior_policy_claim_count == 0
    assert first.prior_insurer_claim_count == 0
    assert first.prior_insurer_average_claim_amount_usd is None
    assert first.claim_to_prior_insurer_average_ratio is None

    assert second.prior_policy_claim_count == 1
    assert second.prior_insurer_claim_count == 1
    assert second.prior_insurer_average_claim_amount_usd == 1000
    assert second.claim_to_prior_insurer_average_ratio == 3

    assert third.prior_policy_claim_count == 0
    assert third.prior_insurer_claim_count == 2
    assert third.prior_insurer_average_claim_amount_usd == 2000
    assert third.claim_to_prior_insurer_average_ratio == 1

    # A Kafka replay after later claims must return the original audit snapshot,
    # not recompute history or overwrite the original duplicate-match count.
    replay = processor.process(
        event(7),
        claim(
            claim_id=7,
            policy_reference="policy-a",
            claim_amount_usd=1000,
        ),
        duplicate_check(with_match=True),
    )
    assert replay == first
    assert replay.cross_insurer_duplicate_match_count == 0
