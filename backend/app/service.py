"""Run the claim workflow across the model, IPFS, and Sepolia."""

from __future__ import annotations

import json
from typing import Protocol

from web3 import Web3

from backend.app.blockchain import (
    BlockchainSubmissionError,
    ChainAssessment,
    ChainClaim,
    ChainSubmission,
    SepoliaClaimsRegistry,
)
from backend.app.models import (
    AssessmentReasonResponse,
    ClaimAssessmentResponse,
    ClaimListItemResponse,
    ClaimPageResponse,
    ClaimSubmission,
    ClaimSubmissionResponse,
)
from listener.ipfs import IPFSClient, IPFSError
from model.scorer import FraudScore, SyntheticFraudScorer


class ClaimSubmissionServiceError(RuntimeError):
    """Raised when the complete IPFS and blockchain operation cannot finish."""


class IPFSStore(Protocol):
    def upload_bytes(
        self, payload: bytes, *, filename: str, content_type: str
    ) -> str: ...

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes: ...


class ClaimsRegistry(Protocol):
    def submit_claim(self, claim_hash: bytes, data_pointer: str) -> ChainSubmission: ...

    def assess_claim(
        self, claim_id: int, status: int, fraud_score: int
    ) -> ChainAssessment: ...

    def list_claims(
        self, *, page: int, page_size: int
    ) -> tuple[list[ChainClaim], int]: ...


class FraudScoring(Protocol):
    def score(self, claim: ClaimSubmission) -> FraudScore: ...


def canonical_claim_bytes(claim: ClaimSubmission) -> bytes:
    """Create stable JSON bytes so the same claim always has the same hash."""

    return json.dumps(
        claim.canonical_document(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class ClaimSubmissionService:
    def __init__(
        self,
        *,
        ipfs: IPFSStore,
        registry: ClaimsRegistry,
        scorer: FraudScoring | None = None,
    ) -> None:
        self.ipfs = ipfs
        self.registry = registry
        self.scorer = scorer or SyntheticFraudScorer.from_env()

    @classmethod
    def from_env(cls) -> "ClaimSubmissionService":
        try:
            return cls(
                ipfs=IPFSClient.from_env(require_upload=True),
                registry=SepoliaClaimsRegistry.from_env(),
                scorer=SyntheticFraudScorer.from_env(),
            )
        except (IPFSError, BlockchainSubmissionError, ValueError) as exc:
            raise ClaimSubmissionServiceError(str(exc)) from exc

    def submit(self, claim: ClaimSubmission) -> ClaimSubmissionResponse:
        # These exact bytes are uploaded to IPFS and hashed for the contract.
        payload = canonical_claim_bytes(claim)
        try:
            # Scoring happens locally; no claim data is sent to another model API.
            model_result = self.scorer.score(claim)
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

        # These numbers match the Status enum in the Solidity contract:
        # 1 = UnderReview and 4 = Flagged.
        assessment_status = 4 if model_result.flagged else 1
        assessment_label = "Flagged" if model_result.flagged else "UnderReview"
        assessment_error: str | None = None
        assessment_transaction: ChainAssessment | None = None
        try:
            assessment_transaction = self.registry.assess_claim(
                chain_result.claim_id,
                assessment_status,
                model_result.score_basis_points,
            )
        except BlockchainSubmissionError as exc:
            # The claim is already safely anchored. Return that successful receipt
            # so a browser retry does not create the same claim again.
            assessment_error = f"On-chain assessment is pending: {exc}"

        return ClaimSubmissionResponse(
            claim_id=chain_result.claim_id,
            transaction_hash=chain_result.transaction_hash,
            block_number=chain_result.block_number,
            data_pointer=data_pointer,
            claim_hash=claim_hash.hex(),
            assessment=ClaimAssessmentResponse(
                status=assessment_label,
                fraud_score=model_result.score_basis_points,
                probability=model_result.probability,
                threshold=model_result.threshold,
                model_version=model_result.model_version,
                reasons=[
                    AssessmentReasonResponse(
                        feature=reason.feature,
                        label=reason.label,
                        contribution=reason.contribution,
                    )
                    for reason in model_result.reasons
                ],
                on_chain=assessment_transaction is not None,
                transaction_hash=(
                    assessment_transaction.transaction_hash
                    if assessment_transaction
                    else None
                ),
                block_number=(
                    assessment_transaction.block_number
                    if assessment_transaction
                    else None
                ),
                error=assessment_error,
            ),
        )

    def list_claims(self, *, page: int, page_size: int) -> ClaimPageResponse:
        """Build one dashboard page from the current contract state."""

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
            raise ClaimSubmissionServiceError(str(exc)) from exc

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
        # Integer division rounds down, so adding page_size - 1 gives us the
        # correct page count when the final page is only partly full.
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        return ClaimPageResponse(
            items=items,
            page=page,
            page_size=page_size,
            total_items=total_items,
            total_pages=total_pages,
        )
