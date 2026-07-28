"""Coordinate the user-facing claim submission without leaking adapter details.

The order is important: create one canonical document, prove that the same bytes
can be read from IPFS, and only then anchor their hash on Sepolia. In asynchronous
mode this service deliberately stops after the anchor; the listener and Kafka
worker own the later model assessment.
"""

from __future__ import annotations

import json
from typing import Protocol

from web3 import Web3

from backend.app.blockchain import (
    BlockchainSubmissionError,
    ChainClaim,
    ChainSubmission,
    SepoliaClaimsRegistry,
)
from backend.app.models import (
    ClaimListItemResponse,
    ClaimPageResponse,
    ClaimSubmission,
    ClaimSubmissionResponse,
)
from integrations.ipfs import IPFSClient, IPFSError


class ClaimSubmissionServiceError(RuntimeError):
    """Raised when the complete IPFS and blockchain operation cannot finish."""


class ClaimQueryServiceError(RuntimeError):
    """Raised when the public claims registry cannot be read."""


class IPFSStore(Protocol):
    def upload_bytes(
        self, payload: bytes, *, filename: str, content_type: str
    ) -> str: ...

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes: ...


class ClaimsRegistryWriter(Protocol):
    def submit_claim(self, claim_hash: bytes, data_pointer: str) -> ChainSubmission: ...


class ClaimsRegistryReader(Protocol):
    def list_claims(
        self, *, page: int, page_size: int
    ) -> tuple[list[ChainClaim], int]: ...


def canonical_claim_bytes(claim: ClaimSubmission) -> bytes:
    """Create stable JSON bytes so the same claim always has the same hash."""

    return json.dumps(
        claim.canonical_document(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class ClaimQueryService:
    """Read public claim history without receiving upload or wallet credentials.

    A blockchain registry is public by design. Keeping this small read service
    separate from submission means opening the dashboard cannot accidentally
    gain access to the Pinata token or transaction-signing account.
    """

    def __init__(self, *, registry: ClaimsRegistryReader) -> None:
        self.registry = registry

    @classmethod
    def from_env(cls) -> "ClaimQueryService":
        """Build the read-only Sepolia client from public configuration."""

        try:
            return cls(
                registry=SepoliaClaimsRegistry.from_env(require_private_key=False)
            )
        except (BlockchainSubmissionError, ValueError) as exc:
            raise ClaimQueryServiceError(str(exc)) from exc

    def list_claims(self, *, page: int, page_size: int) -> ClaimPageResponse:
        """Build one browser page from the current public contract state."""

        # Solidity stores statuses as compact enum numbers. Translate them at
        # this boundary so the browser works with understandable domain words.
        status_names = [
            "Submitted",
            "UnderReview",
            "Approved",
            "Rejected",
            "Flagged",
        ]
        try:
            claims, total_items = self.registry.list_claims(
                page=page, page_size=page_size
            )
        except BlockchainSubmissionError as exc:
            raise ClaimQueryServiceError(str(exc)) from exc

        items = [
            ClaimListItemResponse(
                claim_id=claim.claim_id,
                claimant=claim.claimant,
                claim_hash=claim.claim_hash,
                data_pointer=claim.data_pointer,
                status=(
                    status_names[claim.status]
                    if 0 <= claim.status < len(status_names)
                    else f"Unknown({claim.status})"
                ),
                fraud_score=claim.fraud_score,
                submitted_at=claim.submitted_at,
                updated_at=claim.updated_at,
            )
            for claim in claims
        ]
        # Integer division rounds down. Adding page_size - 1 accounts for a
        # partially filled final page without using floating-point arithmetic.
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        return ClaimPageResponse(
            items=items,
            page=page,
            page_size=page_size,
            total_items=total_items,
            total_pages=total_pages,
        )


class ClaimSubmissionService:
    """Store and anchor a claim before asynchronous assessment begins."""

    def __init__(
        self,
        *,
        ipfs: IPFSStore,
        registry: ClaimsRegistryWriter,
    ) -> None:
        self.ipfs = ipfs
        self.registry = registry

    @classmethod
    def from_env(cls) -> "ClaimSubmissionService":
        try:
            return cls(
                ipfs=IPFSClient.from_env(require_upload=True),
                registry=SepoliaClaimsRegistry.from_env(),
            )
        except (IPFSError, BlockchainSubmissionError, ValueError) as exc:
            raise ClaimSubmissionServiceError(str(exc)) from exc

    def submit(self, claim: ClaimSubmission) -> ClaimSubmissionResponse:
        # These exact bytes are uploaded to IPFS and hashed for the contract.
        payload = canonical_claim_bytes(claim)
        try:
            cid = self.ipfs.upload_bytes(
                payload,
                filename=f"{claim.claim_reference}.json",
                content_type="application/json",
            )
            data_pointer = f"ipfs://{cid}"

            # Read the file back before using its CID. This catches a failed or
            # incomplete upload before anything permanent is written on-chain.
            downloaded = self.ipfs.download_pointer(data_pointer)
            if downloaded != payload:
                raise ClaimSubmissionServiceError(
                    "IPFS round-trip returned bytes different from the uploaded claim"
                )

            # Sepolia stores the small hash and IPFS address, not the full claim.
            claim_hash = Web3.keccak(payload)
            chain_result = self.registry.submit_claim(claim_hash, data_pointer)
        except ClaimSubmissionServiceError:
            raise
        except (IPFSError, BlockchainSubmissionError) as exc:
            raise ClaimSubmissionServiceError(str(exc)) from exc
        except Exception as exc:
            raise ClaimSubmissionServiceError(
                f"Claim submission failed: {exc}"
            ) from exc

        # The anchor is complete. `assessment: null` is the expected pending
        # state while the listener and Kafka worker verify and score the claim.
        return ClaimSubmissionResponse(
            claim_id=chain_result.claim_id,
            transaction_hash=chain_result.transaction_hash,
            block_number=chain_result.block_number,
            data_pointer=data_pointer,
            claim_hash=claim_hash.hex(),
            assessment=None,
        )
