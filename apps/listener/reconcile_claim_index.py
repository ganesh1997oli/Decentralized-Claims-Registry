"""Compare the PostgreSQL claims projection with current contract state.

This command never repairs or mutates the indexed event projection. It appends
only the compact comparison result to the operations audit table so the
authenticated dashboard can report when the index was last verified. A mismatch
should be repaired by stopping the listener, clearing only the affected
deployment projection, and replaying confirmed events from
``LISTENER_START_BLOCK``. Mutating claim rows from a point-in-time contract read
would discard event history and hide the underlying indexing failure.

Run reconciliation while the listener is caught up and temporarily stopped so
an assessment cannot legitimately change halfway through the comparison.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Protocol

from apps.backend.app.blockchain import ChainClaim, SepoliaClaimsRegistry
from packages.integrations.ethereum import ClaimsDeployment, load_claims_deployment
from packages.integrations.postgres import (
    ClaimIndexStatus,
    IndexedClaim,
    PostgresRepositories,
)
from packages.observability import configure_logging, get_event_logger

logger = get_event_logger(__name__)


class ContractClaimReader(Protocol):
    def claim_count(self, *, block_identifier: int | None = None) -> int: ...

    def get_claim(
        self, claim_id: int, *, block_identifier: int | None = None
    ) -> ChainClaim: ...


class IndexedClaimReader(Protocol):
    def get_claim(
        self, *, chain_id: int, contract_address: str, claim_id: int
    ) -> IndexedClaim | None: ...

    def list_claims(
        self,
        *,
        chain_id: int,
        contract_address: str,
        page: int,
        page_size: int,
    ) -> tuple[list[IndexedClaim], int]: ...

    def get_status(
        self, *, chain_id: int, contract_address: str
    ) -> ClaimIndexStatus | None: ...


@dataclass(frozen=True)
class ClaimIndexReconciliation:
    """Small machine-readable result suitable for CI or an operator script."""

    indexed_through_block: int
    chain_claims: int
    indexed_claims: int
    missing_claim_ids: tuple[int, ...]
    unexpected_claim_ids: tuple[int, ...]
    mismatched_claim_ids: tuple[int, ...]

    @property
    def consistent(self) -> bool:
        return not (
            self.missing_claim_ids
            or self.unexpected_claim_ids
            or self.mismatched_claim_ids
        )


def _same_public_state(chain: ChainClaim, indexed: IndexedClaim) -> bool:
    """Compare only fields represented by both authoritative and derived state."""

    return (
        chain.claim_id == indexed.claim_id
        and chain.claimant.lower() == indexed.claimant.lower()
        and chain.claim_hash.lower() == indexed.claim_hash.lower()
        and chain.data_pointer == indexed.data_pointer
        and chain.status == indexed.status
        and chain.fraud_score == indexed.fraud_score
        and chain.submitted_at == indexed.submitted_at
        and chain.updated_at == indexed.updated_at
    )


class ClaimIndexReconciler:
    """Detect missing, unexpected, and stale indexed claims without writing."""

    def __init__(
        self,
        *,
        deployment: ClaimsDeployment,
        contract: ContractClaimReader,
        index: IndexedClaimReader,
    ) -> None:
        self.deployment = deployment
        self.contract = contract
        self.index = index

    def reconcile(self) -> ClaimIndexReconciliation:
        scope = {
            "chain_id": self.deployment.chain_id,
            "contract_address": self.deployment.address,
        }
        status = self.index.get_status(**scope)
        if status is None:
            raise RuntimeError(
                "Claim index has no checkpoint; start the listener and complete "
                "its deployment-block backfill before reconciliation"
            )
        snapshot_block = status.last_processed_block
        # Every contract call is pinned to the database checkpoint. New blocks
        # may arrive while this command runs without creating a false mismatch.
        chain_count = self.contract.claim_count(
            block_identifier=snapshot_block,
        )
        # Bound every database response so reconciliation also works for a large
        # rebuilt registry without loading an unbounded SQL result in one call.
        indexed_by_id: dict[int, IndexedClaim] = {}
        page = 1
        indexed_count = 0
        while page == 1 or len(indexed_by_id) < indexed_count:
            indexed_page, indexed_count = self.index.list_claims(
                **scope,
                page=page,
                page_size=1_000,
            )
            indexed_by_id.update(
                (claim.claim_id, claim) for claim in indexed_page
            )
            if not indexed_page:
                break
            page += 1
        missing: list[int] = []
        mismatched: list[int] = []
        for claim_id in range(chain_count):
            indexed = indexed_by_id.get(claim_id)
            if indexed is None:
                missing.append(claim_id)
                continue
            chain_claim = self.contract.get_claim(
                claim_id,
                block_identifier=snapshot_block,
            )
            if not _same_public_state(chain_claim, indexed):
                mismatched.append(claim_id)

        expected_ids = set(range(chain_count))
        unexpected = sorted(set(indexed_by_id) - expected_ids)
        return ClaimIndexReconciliation(
            indexed_through_block=snapshot_block,
            chain_claims=chain_count,
            indexed_claims=indexed_count,
            missing_claim_ids=tuple(missing),
            unexpected_claim_ids=tuple(unexpected),
            mismatched_claim_ids=tuple(mismatched),
        )


def main() -> None:
    configure_logging("claims-index-reconciler")

    deployment = load_claims_deployment(os.environ)
    repositories = PostgresRepositories.from_env()
    registry = SepoliaClaimsRegistry.from_env(require_private_key=False)
    started_at = time.monotonic()
    result = ClaimIndexReconciler(
        deployment=deployment,
        contract=registry,
        index=repositories.claims,
    ).reconcile()
    duration_ms = max(0, round((time.monotonic() - started_at) * 1_000))
    # This audit append is intentionally distinct from projection repair. Both
    # success and mismatch results are valuable operational facts and neither
    # changes the event history or the current indexed claim state.
    repositories.claims.record_reconciliation(
        chain_id=deployment.chain_id,
        contract_address=deployment.address,
        indexed_through_block=result.indexed_through_block,
        chain_claims=result.chain_claims,
        indexed_claims=result.indexed_claims,
        missing_claim_ids=result.missing_claim_ids,
        unexpected_claim_ids=result.unexpected_claim_ids,
        mismatched_claim_ids=result.mismatched_claim_ids,
        consistent=result.consistent,
        duration_ms=duration_ms,
    )
    output = {
        **asdict(result),
        "consistent": result.consistent,
        "duration_ms": duration_ms,
    }
    print(json.dumps(output, sort_keys=True))
    if not result.consistent:
        logger.error("claim_index.reconciliation_failed", **asdict(result))
        raise SystemExit(1)
    logger.info("claim_index.reconciliation_succeeded", **asdict(result))


if __name__ == "__main__":
    main()
