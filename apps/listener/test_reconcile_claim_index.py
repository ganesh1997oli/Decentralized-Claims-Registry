"""Tests for read-only blockchain/index reconciliation."""

from datetime import UTC, datetime

import pytest

from apps.backend.app.blockchain import ChainClaim
from apps.listener.reconcile_claim_index import ClaimIndexReconciler
from packages.integrations.ethereum import ClaimsDeployment
from packages.integrations.postgres import ClaimIndexStatus, IndexedClaim


def deployment() -> ClaimsDeployment:
    return ClaimsDeployment(
        deployment_id="test-deployment",
        chain_id=11_155_111,
        address="0xContract",
        abi=(),
    )


def claim(claim_id: int, *, status: int = 1) -> IndexedClaim:
    return IndexedClaim(
        claim_id=claim_id,
        claimant="0xClaimant",
        claim_hash=f"0xhash{claim_id}",
        data_pointer=f"ipfs://claim-{claim_id}",
        status=status,
        fraud_score=4_200,
        submitted_at=1_750_000_000 + claim_id,
        updated_at=1_750_000_100 + claim_id,
    )


def chain_claim(indexed: IndexedClaim) -> ChainClaim:
    return ChainClaim(**indexed.__dict__)


class FakeContract:
    def __init__(self, claims):
        self.claims = claims
        self.snapshot_blocks = []

    def claim_count(self, *, block_identifier=None):
        self.snapshot_blocks.append(block_identifier)
        return len(self.claims)

    def get_claim(self, claim_id, *, block_identifier=None):
        self.snapshot_blocks.append(block_identifier)
        return self.claims[claim_id]


class FakeIndex:
    def __init__(self, claims):
        self.claims = claims

    def get_claim(self, *, chain_id, contract_address, claim_id):
        assert chain_id == 11_155_111
        assert contract_address == "0xContract"
        return next(
            (claim for claim in self.claims if claim.claim_id == claim_id),
            None,
        )

    def list_claims(
        self, *, chain_id, contract_address, page, page_size
    ):
        assert chain_id == 11_155_111
        assert contract_address == "0xContract"
        start = (page - 1) * page_size
        return self.claims[start : start + page_size], len(self.claims)

    def get_status(self, *, chain_id, contract_address):
        return ClaimIndexStatus(
            chain_id=chain_id,
            contract_address=contract_address,
            last_processed_block=120,
            updated_at=datetime(2026, 8, 4, tzinfo=UTC),
        )


def test_reconciliation_accepts_an_exact_projection():
    indexed = [claim(1), claim(0)]
    contract = FakeContract([chain_claim(indexed[1]), chain_claim(indexed[0])])

    result = ClaimIndexReconciler(
        deployment=deployment(),
        contract=contract,
        index=FakeIndex(indexed),
    ).reconcile()

    assert result.consistent
    assert result.chain_claims == 2
    assert result.indexed_claims == 2
    assert result.indexed_through_block == 120
    assert contract.snapshot_blocks == [120, 120, 120]


def test_reconciliation_reports_missing_unexpected_and_stale_claims():
    authoritative = [chain_claim(claim(0)), chain_claim(claim(1, status=4))]
    indexed = [claim(9), claim(1, status=1)]

    result = ClaimIndexReconciler(
        deployment=deployment(),
        contract=FakeContract(authoritative),
        index=FakeIndex(indexed),
    ).reconcile()

    assert not result.consistent
    assert result.missing_claim_ids == (0,)
    assert result.unexpected_claim_ids == (9,)
    assert result.mismatched_claim_ids == (1,)


def test_reconciliation_requires_an_initialized_checkpoint():
    class UninitializedIndex(FakeIndex):
        def get_status(self, *, chain_id, contract_address):
            return None

    with pytest.raises(RuntimeError, match="no checkpoint"):
        ClaimIndexReconciler(
            deployment=deployment(),
            contract=FakeContract([]),
            index=UninitializedIndex([]),
        ).reconcile()
