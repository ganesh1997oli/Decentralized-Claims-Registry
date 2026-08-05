"""PostgreSQL integration coverage for event replay and indexed pagination."""

import pytest

pytestmark = pytest.mark.integration


def submission(claim_id: int, block_number: int) -> dict:
    return {
        "chain_id": 11_155_111,
        "contract_address": "0x1111111111111111111111111111111111111111",
        "claim_id": claim_id,
        "claimant": "0x2222222222222222222222222222222222222222",
        "claim_hash": f"0xhash{claim_id}",
        "data_pointer": f"ipfs://claim-{claim_id}",
        "block_number": block_number,
        "block_hash": f"0xblock{block_number}",
        "transaction_hash": f"0xsubmission{claim_id}",
        "log_index": 0,
        "event_timestamp": 1_750_000_000 + claim_id,
    }


def test_replay_safe_projection_preserves_latest_state(postgres_repositories):
    repository = postgres_repositories.claims
    first = submission(7, 100)
    second = submission(8, 101)

    repository.index_claim_submitted(**first)
    repository.index_claim_submitted(**second)
    repository.index_claim_assessed(
        chain_id=first["chain_id"],
        contract_address=first["contract_address"],
        claim_id=7,
        status=4,
        fraud_score=8_500,
        block_number=102,
        block_hash="0xassessmentblock",
        transaction_hash="0xassessment7",
        log_index=1,
        event_timestamp=1_750_000_100,
    )

    # Replaying both the assessment and its older submission is expected after
    # a crash before checkpoint persistence. Neither operation may regress the
    # current projected state or create another audit event.
    repository.index_claim_assessed(
        chain_id=first["chain_id"],
        contract_address=first["contract_address"],
        claim_id=7,
        status=4,
        fraud_score=8_500,
        block_number=102,
        block_hash="0xassessmentblock",
        transaction_hash="0xassessment7",
        log_index=1,
        event_timestamp=1_750_000_100,
    )
    repository.index_claim_submitted(**first)

    claims, total = repository.list_claims(
        chain_id=first["chain_id"],
        contract_address=first["contract_address"],
        page=1,
        page_size=10,
    )
    assert total == 2
    assert [claim.claim_id for claim in claims] == [8, 7]
    assert claims[1].status == 4
    assert claims[1].fraud_score == 8_500
    assert claims[1].updated_at == 1_750_000_100

    repository.save_checkpoint(
        chain_id=first["chain_id"],
        contract_address=first["contract_address"],
        block_number=102,
    )
    repository.record_reconciliation(
        chain_id=first["chain_id"],
        contract_address=first["contract_address"],
        indexed_through_block=102,
        chain_claims=2,
        indexed_claims=2,
        missing_claim_ids=(),
        unexpected_claim_ids=(),
        mismatched_claim_ids=(),
        consistent=True,
        duration_ms=12,
    )
    operations = repository.get_operations_snapshot(
        chain_id=first["chain_id"],
        contract_address=first["contract_address"],
        recent_event_limit=10,
    )
    assert operations.total_claims == 2
    assert operations.total_events == 3
    assert operations.submitted_events == 2
    assert operations.assessed_events == 1
    assert operations.claim_status_counts == (1, 0, 0, 0, 1)
    assert operations.checkpoint is not None
    assert operations.checkpoint.last_processed_block == 102
    assert len(operations.recent_events) == 3
    assert operations.last_reconciliation is not None
    assert operations.last_reconciliation.consistent is True

    first_event_page = repository.search_events(
        chain_id=first["chain_id"],
        contract_address=first["contract_address"],
        claim_id=7,
        limit=1,
    )
    assert [event.event_type for event in first_event_page.events] == ["ClaimAssessed"]
    assert first_event_page.has_more is True

    first_event = first_event_page.events[0]
    older_event_page = repository.search_events(
        chain_id=first["chain_id"],
        contract_address=first["contract_address"],
        claim_id=7,
        before=(
            first_event.block_number,
            first_event.log_index,
            first_event.event_id,
        ),
        limit=1,
    )
    assert [event.event_type for event in older_event_page.events] == ["ClaimSubmitted"]
    assert older_event_page.has_more is False

    flagged_events = repository.search_events(
        chain_id=first["chain_id"],
        contract_address=first["contract_address"],
        event_type="ClaimAssessed",
        status=4,
        from_block=102,
        to_block=102,
        limit=10,
    )
    assert [event.claim_id for event in flagged_events.events] == [7]

    with postgres_repositories.database.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS count FROM claim_index_events")
        assert cursor.fetchone()["count"] == 3
