"""Coordinate the user-facing claim submission without leaking adapter details.

The order is important: create one canonical document, prove that the same bytes
can be read from IPFS, and only then anchor their hash on Sepolia. In asynchronous
mode this service stops after the anchor; the listener and Kafka
worker own the later model assessment.
"""

from __future__ import annotations

import os
from typing import Protocol

from web3 import Web3

from apps.backend.app.blockchain import (
    BlockchainSubmissionError,
    ChainSubmission,
    SepoliaClaimsRegistry,
)
from apps.backend.app.models import (
    ClaimListItemResponse,
    ClaimPageResponse,
    ClaimSubmission,
    ClaimSubmissionResponse,
)
from apps.backend.app.policy_eligibility import ClaimantPrincipal
from apps.backend.app.submission_auth import (
    ClaimAuthorizationSigner,
    InsurerPrincipal,
    SubmissionAuthConfigurationError,
)
from packages.integrations.ethereum import (
    DeploymentConfigurationError,
    load_claims_deployment,
)
from packages.integrations.ipfs import IPFSClient, IPFSError
from packages.integrations.postgres import (
    ClaimIndexStatus,
    IndexedClaim,
    PostgresConfigurationError,
    PostgresRepositories,
    PostgresStorageError,
)


class ClaimSubmissionServiceError(RuntimeError):
    """Raised when the complete IPFS and blockchain operation cannot finish."""


class ClaimQueryServiceError(RuntimeError):
    """Raised when the public claims registry cannot be read."""


class IPFSStore(Protocol):
    """Content-addressed storage used during submission."""

    def upload_bytes(self, payload: bytes, *, filename: str, content_type: str) -> str:
        """Upload exact bytes and return their content identifier."""

        ...

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes:
        """Resolve an IPFS pointer to exact bytes with bounded retries."""

        ...


class ClaimsRegistryWriter(Protocol):
    """Minimal on-chain write capability required by claim submission."""

    def submit_claim(self, claim_hash: bytes, data_pointer: str) -> ChainSubmission:
        """Anchor a hash/pointer pair and return the confirmed contract receipt."""

        ...


class ClaimsIndexReader(Protocol):
    """Read current claims and progress from the rebuildable event projection."""

    def list_claims(
        self,
        *,
        chain_id: int,
        contract_address: str,
        page: int,
        page_size: int,
    ) -> tuple[list[IndexedClaim], int]:
        """Return one bounded newest-first page and its total row count."""

        ...

    def get_status(
        self, *, chain_id: int, contract_address: str
    ) -> ClaimIndexStatus | None:
        """Return the projection checkpoint or ``None`` before first progress."""

        ...


def canonical_claim_bytes(
    claim: ClaimSubmission,
    principal: InsurerPrincipal | ClaimantPrincipal,
    authorization: ClaimAuthorizationSigner,
) -> bytes:
    """Create the one authorized byte representation used by IPFS and Sepolia.

    Canonicalization, insurer binding, and the authorization signature happen
    before either external write. The returned bytes must be uploaded and hashed
    unchanged or the listener's later Keccak verification will reject the event.
    """

    return authorization.authorized_claim_bytes(claim, principal)


class ClaimQueryService:
    """Read the confirmed-event projection without wallet or upload credentials.

    PostgreSQL is a rebuildable query layer; the on-chain record remains primary.
    Keeping it separate from submission means dashboard reads remain fast and
    cannot accidentally gain access to Pinata or a transaction-signing account.
    """

    def __init__(
        self,
        *,
        index: ClaimsIndexReader,
        chain_id: int,
        contract_address: str,
    ) -> None:
        """Bind the query service permanently to one chain/contract projection.

        The injected interface excludes upload and signing operations. Deployment
        scope is stored once so no request can choose another contract through
        user-controlled query parameters.
        """

        self.index = index
        self.chain_id = chain_id
        self.contract_address = contract_address

    @classmethod
    def from_env(cls) -> ClaimQueryService:
        """Build the deployment-scoped PostgreSQL read path.

        Adapter-specific configuration/storage failures are normalized to the
        service exception translated by FastAPI into a 503 response.
        """

        try:
            deployment = load_claims_deployment(os.environ)
            repositories = PostgresRepositories.from_env()
            return cls(
                index=repositories.claims,
                chain_id=deployment.chain_id,
                contract_address=deployment.address,
            )
        except (
            DeploymentConfigurationError,
            PostgresConfigurationError,
            PostgresStorageError,
            ValueError,
        ) as exc:
            raise ClaimQueryServiceError(str(exc)) from exc

    def list_claims(self, *, page: int, page_size: int) -> ClaimPageResponse:
        """Build one browser page from the confirmed blockchain index.

        The projection supplies current claim rows, count, and index progress
        without an RPC scan. Numeric Solidity statuses are translated here so
        persistence remains contract-shaped while the API stays domain-readable.
        Unknown future enum values remain visible rather than failing the page.
        """

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
            claims, total_items = self.index.list_claims(
                chain_id=self.chain_id,
                contract_address=self.contract_address,
                page=page,
                page_size=page_size,
            )
            index_status = self.index.get_status(
                chain_id=self.chain_id,
                contract_address=self.contract_address,
            )
        except PostgresStorageError as exc:
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
            indexed_through_block=(
                index_status.last_processed_block if index_status is not None else None
            ),
        )


class ClaimSubmissionService:
    """Store and anchor a claim before asynchronous assessment begins."""

    def __init__(
        self,
        *,
        ipfs: IPFSStore,
        registry: ClaimsRegistryWriter,
        authorization: ClaimAuthorizationSigner,
    ) -> None:
        """Bind upload, contract-write, and authorization adapters.

        Keeping these write-capable dependencies separate from ``ClaimQueryService``
        prevents ordinary dashboard reads from acquiring Pinata or wallet authority.
        """

        self.ipfs = ipfs
        self.registry = registry
        self.authorization = authorization

    @classmethod
    def from_env(cls) -> ClaimSubmissionService:
        """Build the production write workflow and normalize unsafe configuration.

        Construction validates upload credentials, the hardened deployment,
        submitter role, and claim-authorization signer before accepting a request.
        """

        try:
            return cls(
                ipfs=IPFSClient.from_env(require_upload=True),
                registry=SepoliaClaimsRegistry.from_env(
                    private_key_env="SEPOLIA_SUBMITTER_PRIVATE_KEY"
                ),
                authorization=ClaimAuthorizationSigner.from_env(),
            )
        except (
            IPFSError,
            BlockchainSubmissionError,
            SubmissionAuthConfigurationError,
            ValueError,
        ) as exc:
            raise ClaimSubmissionServiceError(str(exc)) from exc

    def submit(
        self,
        claim: ClaimSubmission,
        principal: InsurerPrincipal,
    ) -> ClaimSubmissionResponse:
        """Authorize, pin, verify, and anchor one claim in irreversible order.

        The exact canonical bytes are read back from IPFS before their Keccak hash
        is sent to Sepolia. A successful return means the anchor was mined and its
        ``ClaimSubmitted`` event decoded; model assessment remains asynchronous and
        is represented by ``assessment=None``.
        """

        # These exact bytes are uploaded to IPFS and hashed for the contract.
        payload = canonical_claim_bytes(claim, principal, self.authorization)
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
