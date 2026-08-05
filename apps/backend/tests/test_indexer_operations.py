"""Tests for authenticated indexer telemetry and health classification."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apps.backend.app.indexer_operations import (
    IndexerOperationsAuthenticationError,
    IndexerOperationsBoundary,
    IndexerOperationsService,
)
from apps.backend.app.main import (
    app,
    get_indexer_operations_boundary,
    get_indexer_operations_service,
)
from apps.backend.app.models import IndexerOperationsResponse
from packages.integrations.ethereum import ClaimsDeployment
from packages.integrations.postgres import (
    ClaimIndexEventRecord,
    ClaimIndexOperationsSnapshot,
    ClaimIndexReconciliationRecord,
    ClaimIndexStatus,
)

OPERATIONS_KEY = "test-operations-key-with-high-entropy-material"
OPERATIONS_DIGEST = hashlib.sha256(OPERATIONS_KEY.encode()).hexdigest()


def deployment() -> ClaimsDeployment:
    return ClaimsDeployment(
        deployment_id="test-deployment",
        chain_id=11_155_111,
        address="0xContract",
        abi=(),
    )


class FakeHead:
    def __init__(self, block_number=1_012, error=None):
        self.block_number = block_number
        self.error = error

    def latest_block_number(self):
        if self.error:
            raise self.error
        return self.block_number


class FakeIndex:
    def __init__(self, *, checkpoint_block=1_000, checkpoint_age_seconds=10):
        now = datetime.now(UTC)
        self.snapshot = ClaimIndexOperationsSnapshot(
            checkpoint=(
                ClaimIndexStatus(
                    chain_id=11_155_111,
                    contract_address="0xcontract",
                    last_processed_block=checkpoint_block,
                    updated_at=now - timedelta(seconds=checkpoint_age_seconds),
                )
                if checkpoint_block is not None
                else None
            ),
            total_claims=7,
            total_events=12,
            submitted_events=7,
            assessed_events=5,
            claim_status_counts=(2, 1, 1, 1, 2),
            recent_events=(
                ClaimIndexEventRecord(
                    event_id="11155111:0xtx:0",
                    claim_id=6,
                    event_type="ClaimAssessed",
                    block_number=999,
                    transaction_hash="0xtx",
                    log_index=0,
                    event_timestamp=1_754_395_200,
                    status=4,
                    fraud_score=8_500,
                    indexed_at=now,
                ),
            ),
            last_reconciliation=ClaimIndexReconciliationRecord(
                indexed_through_block=1_000,
                chain_claims=7,
                indexed_claims=7,
                missing_claim_ids=(),
                unexpected_claim_ids=(),
                mismatched_claim_ids=(),
                consistent=True,
                duration_ms=24,
                checked_at=now,
            ),
        )

    def get_operations_snapshot(
        self, *, chain_id, contract_address, recent_event_limit=20
    ):
        assert chain_id == 11_155_111
        assert contract_address == "0xContract"
        assert recent_event_limit == 20
        return self.snapshot


def service(*, index=None, chain=None) -> IndexerOperationsService:
    return IndexerOperationsService(
        deployment=deployment(),
        index=index or FakeIndex(),
        chain=chain or FakeHead(),
        confirmation_blocks=12,
        stale_after_seconds=120,
    )


def test_operations_boundary_uses_digest_and_constant_time_comparison():
    boundary = IndexerOperationsBoundary(OPERATIONS_DIGEST)

    boundary.authenticate(OPERATIONS_KEY)
    with pytest.raises(IndexerOperationsAuthenticationError, match="required"):
        boundary.authenticate(None)
    with pytest.raises(IndexerOperationsAuthenticationError, match="invalid"):
        boundary.authenticate("wrong-key")


def test_operations_snapshot_reports_a_caught_up_index():
    result = service().snapshot()

    assert result.state == "healthy"
    assert result.rpc_status == "available"
    assert result.latest_block == 1_012
    assert result.safe_block == 1_000
    assert result.indexed_through_block == 1_000
    assert result.block_lag == 0
    assert result.total_claims == 7
    assert result.claim_status_counts.flagged == 2
    assert result.recent_events[0].status == "Flagged"
    assert result.last_reconciliation is not None
    assert result.last_reconciliation.consistent is True


def test_operations_snapshot_distinguishes_stall_from_rpc_degradation():
    stalled = service(
        index=FakeIndex(checkpoint_block=900, checkpoint_age_seconds=180),
    ).snapshot()
    degraded = service(chain=FakeHead(error=RuntimeError("offline"))).snapshot()

    assert stalled.state == "stalled"
    assert stalled.block_lag == 100
    assert degraded.state == "degraded"
    assert degraded.rpc_status == "unavailable"
    assert degraded.latest_block is None
    assert degraded.total_events == 12


class FakeOperationsService:
    def snapshot(self) -> IndexerOperationsResponse:
        return service().snapshot()


def test_operations_route_requires_key_and_returns_authenticated_snapshot():
    app.dependency_overrides[get_indexer_operations_boundary] = lambda: (
        IndexerOperationsBoundary(OPERATIONS_DIGEST)
    )
    app.dependency_overrides[get_indexer_operations_service] = FakeOperationsService
    client = TestClient(app)
    try:
        missing = client.get("/operations/indexer")
        invalid = client.get(
            "/operations/indexer",
            headers={"X-Operations-API-Key": "wrong"},
        )
        valid = client.get(
            "/operations/indexer",
            headers={"X-Operations-API-Key": OPERATIONS_KEY},
        )
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert valid.status_code == 200
    assert valid.json()["state"] == "healthy"
    assert valid.json()["last_reconciliation"]["consistent"] is True
