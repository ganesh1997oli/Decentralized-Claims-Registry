"""Application service joining validation, IPFS, and the claims registry."""

from __future__ import annotations

import json
from typing import Protocol

from web3 import Web3

from backend.app.blockchain import (
    BlockchainSubmissionError,
    ChainSubmission,
    SepoliaClaimsRegistry,
)
from backend.app.models import ClaimSubmission, ClaimSubmissionResponse
from listener.ipfs_client import IPFSClient, IPFSError


class ClaimSubmissionServiceError(RuntimeError):
    """Raised when the complete IPFS and blockchain operation cannot finish."""


class IPFSStore(Protocol):
    def upload_bytes(
        self, payload: bytes, *, filename: str, content_type: str
    ) -> str: ...

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes: ...


class ClaimsRegistry(Protocol):
    def submit_claim(self, claim_hash: bytes, data_pointer: str) -> ChainSubmission: ...


def canonical_claim_bytes(claim: ClaimSubmission) -> bytes:
    """Serialize a claim deterministically before hashing and uploading it."""

    return json.dumps(
        claim.canonical_document(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class ClaimSubmissionService:
    def __init__(self, *, ipfs: IPFSStore, registry: ClaimsRegistry) -> None:
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
        payload = canonical_claim_bytes(claim)
        try:
            cid = self.ipfs.upload_bytes(
                payload,
                filename=f"{claim.claim_reference}.json",
                content_type="application/json",
            )
            data_pointer = f"ipfs://{cid}"

            # Do not anchor a pointer until the exact bytes are retrievable.
            downloaded = self.ipfs.download_pointer(data_pointer)
            if downloaded != payload:
                raise ClaimSubmissionServiceError(
                    "IPFS round-trip returned bytes different from the uploaded claim"
                )

            claim_hash = Web3.keccak(payload)
            chain_result = self.registry.submit_claim(claim_hash, data_pointer)
        except ClaimSubmissionServiceError:
            raise
        except (IPFSError, BlockchainSubmissionError) as exc:
            raise ClaimSubmissionServiceError(str(exc)) from exc
        except Exception as exc:
            raise ClaimSubmissionServiceError(f"Claim submission failed: {exc}") from exc

        return ClaimSubmissionResponse(
            claim_id=chain_result.claim_id,
            transaction_hash=chain_result.transaction_hash,
            block_number=chain_result.block_number,
            data_pointer=data_pointer,
            claim_hash=claim_hash.hex(),
        )
