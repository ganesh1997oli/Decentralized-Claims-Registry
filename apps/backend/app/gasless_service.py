"""Two-stage claim preparation and insurer authorization workflow.

The service is the application-level coordinator used by FastAPI. A normal
submission moves through these durable states::

    preparing -> prepared -> authorized -> signed -> broadcast -> confirmed

``prepare`` authenticates the insurer, pins canonical claim bytes to IPFS, and
returns EIP-712 data for the wallet. ``authorize`` verifies and stores the
wallet signature. The later states are owned by the separate relayer, so this
module never holds the payer key or broadcasts an Ethereum transaction.

Every externally visible retry is tied to a credential-scoped idempotency key.
That lets a browser safely repeat an uncertain HTTP request while PostgreSQL
decides whether to resume the existing workflow or reject changed claim data.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from web3 import Web3

from apps.backend.app.gasless_blockchain import (
    GaslessBlockchainError,
    GaslessClaimsGateway,
    PreparedForwardRequest,
)
from apps.backend.app.models import (
    ClaimSubmission,
    ClaimSubmissionResponse,
    EIP712TypedData,
    GaslessNetworkResponse,
    GaslessSubmissionResponse,
)
from apps.backend.app.service import IPFSStore, canonical_claim_bytes
from apps.backend.app.submission_auth import (
    ClaimAuthorizationSigner,
    InsurerPrincipal,
    SubmissionAuthConfigurationError,
)
from packages.integrations.ipfs import IPFSClient, IPFSError
from packages.integrations.postgres import (
    GaslessSubmissionConflictError,
    GaslessSubmissionLimitError,
    GaslessSubmissionNotFoundError,
    GaslessSubmissionRecord,
    PostgresDatabase,
    PostgresGaslessSubmissionRepository,
    PostgresStorageError,
)
from packages.observability import get_event_logger

_MINIMUM_FINGERPRINT_KEY_BYTES = 32
logger = get_event_logger(__name__)


class GaslessSubmissionServiceError(RuntimeError):
    """Raised when a gasless preparation or authorization cannot complete."""


class GaslessSubmissionAccessError(PermissionError):
    """Raised when an insurer cannot access a submission identifier."""


class GaslessSubmissionStateError(RuntimeError):
    """Raised when a request conflicts with durable submission state."""


class GaslessSubmissionRateLimitError(RuntimeError):
    """Raised when durable sponsor limits reject a new preparation."""

    def __init__(self, message: str, *, retry_after: int) -> None:
        """Carry the retry interval used to populate FastAPI's response header."""

        super().__init__(message)
        self.retry_after = retry_after


class GaslessSubmissionStore(Protocol):
    """Persistence boundary required by the keyless HTTP workflow.

    The concrete PostgreSQL adapter owns concurrency and state-transition rules;
    this protocol keeps orchestration independently testable without weakening
    those guarantees in production.
    """

    def begin_preparation(self, **values) -> tuple[GaslessSubmissionRecord, bool]:
        """Reserve or idempotently return one insurer preparation."""

        ...

    def mark_prepared(self, submission_id: UUID, **values) -> GaslessSubmissionRecord:
        """Persist the exact request after IPFS and chain preparation succeed."""

        ...

    def mark_preparation_failed(self, submission_id: UUID, *, error_code: str) -> None:
        """Release a preparation lease through an auditable terminal state."""

        ...

    def get_for_credential(
        self, submission_id: UUID, *, credential_id: str
    ) -> GaslessSubmissionRecord:
        """Read a submission only inside its authenticated credential scope."""

        ...

    def authorize(self, submission_id: UUID, **values) -> GaslessSubmissionRecord:
        """Durably record a verified insurer signature exactly once."""

        ...


def _positive_int(settings: Mapping[str, str], name: str, default: int) -> int:
    """Parse a positive quota or rate value and reject unsafe configuration."""

    try:
        value = int(settings.get(name, str(default)))
    except ValueError as exc:
        raise SubmissionAuthConfigurationError(
            f"{name} must be a positive integer"
        ) from exc
    if value < 1:
        raise SubmissionAuthConfigurationError(f"{name} must be a positive integer")
    return value


def _strict_bool(settings: Mapping[str, str], name: str, default: str) -> bool:
    """Parse an explicit boolean without treating arbitrary text as truthy."""

    value = settings.get(name, default).strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise SubmissionAuthConfigurationError(f"{name} must be true or false")


class GaslessClaimSubmissionService:
    """Coordinate IPFS, EIP-712, idempotency, and sponsorship without a payer key.

    The API phase prepares content and validates insurer intent. It never signs
    an Ethereum transaction; the isolated relayer consumes only records that
    reach the durable ``authorized`` state. Methods in this class orchestrate
    adapters, while concurrency, quota counting, and compare-and-set state
    transitions remain inside the PostgreSQL repository.
    """

    def __init__(
        self,
        *,
        ipfs: IPFSStore,
        chain: GaslessClaimsGateway,
        store: GaslessSubmissionStore,
        authorization: ClaimAuthorizationSigner,
        fingerprint_key: bytes,
        insurer_minute_limit: int,
        client_minute_limit: int,
        allow_rate_limit_bypass: bool,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_submission_id: Callable[[], UUID] = uuid4,
    ) -> None:
        """Inject adapters and enforce minimum secret strength for fingerprints.

        The keyed fingerprints let PostgreSQL compare idempotency, request, and
        client identities without retaining raw API keys, client addresses, or
        full claim JSON in the sponsorship-control tables.
        """

        if len(fingerprint_key) < _MINIMUM_FINGERPRINT_KEY_BYTES:
            raise SubmissionAuthConfigurationError(
                "GASLESS_REQUEST_FINGERPRINT_KEY must contain at least 32 bytes"
            )
        self.ipfs = ipfs
        self.chain = chain
        self.store = store
        self.authorization = authorization
        self.fingerprint_key = fingerprint_key
        self.insurer_minute_limit = insurer_minute_limit
        self.client_minute_limit = client_minute_limit
        self.allow_rate_limit_bypass = allow_rate_limit_bypass
        self.clock = clock
        self.new_submission_id = new_submission_id

    @classmethod
    def from_mapping(
        cls,
        settings: Mapping[str, str],
    ) -> GaslessClaimSubmissionService:
        """Build production adapters from explicit, validated configuration.

        Startup fails when IPFS, PostgreSQL, deployment, authorization, or HMAC
        settings are incomplete. This prevents the HTTP server from appearing
        ready with only part of the durable workflow available.
        """

        raw_fingerprint_key = settings.get("GASLESS_REQUEST_FINGERPRINT_KEY", "")
        if not raw_fingerprint_key:
            raise SubmissionAuthConfigurationError(
                "GASLESS_REQUEST_FINGERPRINT_KEY is required"
            )
        database_url = settings.get("DATABASE_URL", "").strip()
        if not database_url:
            raise SubmissionAuthConfigurationError(
                "DATABASE_URL is required for durable gasless submissions"
            )
        pinata_jwt = settings.get("PINATA_JWT", "").strip()
        if not pinata_jwt:
            raise SubmissionAuthConfigurationError(
                "PINATA_JWT is required for gasless claim preparation"
            )
        try:
            return cls(
                ipfs=IPFSClient(
                    pinata_jwt=pinata_jwt,
                    gateway=settings.get(
                        "IPFS_GATEWAY", "https://gateway.pinata.cloud/ipfs"
                    ),
                ),
                chain=GaslessClaimsGateway.from_mapping(settings),
                store=PostgresGaslessSubmissionRepository(
                    PostgresDatabase(database_url)
                ),
                authorization=ClaimAuthorizationSigner.from_mapping(settings),
                fingerprint_key=raw_fingerprint_key.encode("utf-8"),
                insurer_minute_limit=_positive_int(
                    settings, "INSURER_RATE_LIMIT_PER_MINUTE", 5
                ),
                client_minute_limit=_positive_int(
                    settings, "IP_RATE_LIMIT_PER_MINUTE", 20
                ),
                allow_rate_limit_bypass=_strict_bool(
                    settings, "ALLOW_RATE_LIMIT_BYPASS", "false"
                ),
            )
        except (IPFSError, GaslessBlockchainError, PostgresStorageError) as exc:
            raise GaslessSubmissionServiceError(str(exc)) from exc

    @classmethod
    def from_env(cls) -> GaslessClaimSubmissionService:
        """Construct the service from the current FastAPI process environment."""

        return cls.from_mapping(os.environ)

    def _fingerprint(self, namespace: str, value: str) -> str:
        """Create a domain-separated HMAC for privacy-preserving equality checks."""

        return hmac.new(
            self.fingerprint_key,
            f"{namespace}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def network(self) -> GaslessNetworkResponse:
        """Return the deployment identity the browser verifies before signing.

        Fetching this first lets the UI switch networks and later detect a
        configuration change between discovery and preparation.
        """

        forwarder_address = self.chain.deployment.forwarder_address
        if forwarder_address is None:
            raise GaslessSubmissionServiceError(
                "The selected claims deployment has no trusted forwarder"
            )
        return GaslessNetworkResponse(
            chain_id=self.chain.deployment.chain_id,
            contract_address=self.chain.deployment.address,
            forwarder_address=forwarder_address,
        )

    def prepare(
        self,
        claim: ClaimSubmission,
        principal: InsurerPrincipal,
        *,
        idempotency_key: str,
        client_ip: str,
    ) -> GaslessSubmissionResponse:
        """Reserve sponsorship, verify IPFS bytes, and prepare EIP-712 data.

        ``begin_preparation`` makes the request idempotent and enforces durable
        quotas before a paid Pinata operation. The exact canonical payload is
        uploaded, downloaded, byte-compared, hashed, and encoded into the one
        forward request the insurer may authorize. A matching retry returns the
        existing record instead of repeating side effects.
        """

        now = self.clock()
        if now.tzinfo is None:
            raise GaslessSubmissionServiceError(
                "Gasless submission clock must be timezone-aware"
            )
        try:
            signer = self.chain.validate_signer(principal.signer_address)
            submission_id = self.new_submission_id()
            record, created = self.store.begin_preparation(
                submission_id=submission_id,
                credential_id=principal.credential_id,
                insurer_id=principal.insurer_id,
                signer_address=signer,
                chain_id=self.chain.deployment.chain_id,
                contract_address=self.chain.deployment.address,
                forwarder_address=self.chain.deployment.forwarder_address,
                idempotency_key_hash=self._fingerprint("idempotency", idempotency_key),
                request_fingerprint=self._fingerprint(
                    "claim", claim.model_dump_json(by_alias=True)
                ),
                client_fingerprint=self._fingerprint(
                    "client", client_ip.strip() or "unknown"
                ),
                insurer_minute_limit=self.insurer_minute_limit,
                client_minute_limit=self.client_minute_limit,
                daily_quota=principal.daily_quota,
                bypass_limits=(
                    self.allow_rate_limit_bypass and principal.rate_limit_exempt
                ),
                now=now,
            )
        except GaslessSubmissionLimitError as exc:
            raise GaslessSubmissionRateLimitError(
                str(exc), retry_after=exc.retry_after
            ) from exc
        except GaslessSubmissionConflictError as exc:
            raise GaslessSubmissionStateError(str(exc)) from exc
        except (GaslessBlockchainError, PostgresStorageError, ValueError) as exc:
            raise GaslessSubmissionServiceError(str(exc)) from exc

        if not created:
            return self._response(record)

        try:
            payload = canonical_claim_bytes(claim, principal, self.authorization)
            cid = self.ipfs.upload_bytes(
                payload,
                filename=f"{claim.claim_reference}.json",
                content_type="application/json",
            )
            data_pointer = f"ipfs://{cid}"
            if self.ipfs.download_pointer(data_pointer) != payload:
                raise GaslessSubmissionServiceError(
                    "IPFS round-trip returned bytes different from the uploaded claim"
                )
            claim_hash = Web3.keccak(payload)
            request = self.chain.prepare_request(
                signer_address=signer,
                claim_hash=claim_hash,
                data_pointer=data_pointer,
            )
            record = self.store.mark_prepared(
                record.submission_id,
                claim_hash=f"0x{claim_hash.hex().removeprefix('0x')}",
                data_pointer=data_pointer,
                call_data=request.data,
                forwarder_nonce=request.nonce,
                forward_gas=request.gas,
                deadline=request.deadline,
            )
        except Exception as exc:
            try:
                self.store.mark_preparation_failed(
                    record.submission_id,
                    error_code="preparation_failed",
                )
            except Exception as persistence_exc:  # noqa: BLE001
                logger.warning(
                    "gasless.preparation_failure_not_persisted",
                    submission_id=str(record.submission_id),
                    exception_type=type(persistence_exc).__name__,
                )
            if isinstance(exc, GaslessSubmissionServiceError):
                raise
            if isinstance(
                exc,
                (IPFSError, GaslessBlockchainError, PostgresStorageError),
            ):
                raise GaslessSubmissionServiceError(str(exc)) from exc
            raise GaslessSubmissionServiceError(
                "Gasless claim preparation failed"
            ) from exc
        return self._response(record)

    def authorize(
        self,
        submission_id: UUID,
        signature: str,
        principal: InsurerPrincipal,
    ) -> GaslessSubmissionResponse:
        """Verify insurer intent and atomically expose the request to the relayer.

        Credential ownership and signer address are checked before the deployed
        forwarder verifies EIP-712 domain, calldata, nonce, and deadline. Only
        then can PostgreSQL move ``prepared`` to ``authorized``. Repeating the
        same signature is idempotent; submitting a different one conflicts.
        """

        now = self.clock()
        try:
            record = self.store.get_for_credential(
                submission_id,
                credential_id=principal.credential_id,
            )
            if record.signer_address.lower() != principal.signer_address.lower():
                raise GaslessSubmissionAccessError(
                    "Gasless submission does not belong to this insurer signer"
                )
            if record.state == "prepared":
                self.chain.verify_signature(record, signature)
            record = self.store.authorize(
                submission_id,
                credential_id=principal.credential_id,
                signature=signature,
                now=now,
            )
            return self._response(record)
        except GaslessSubmissionNotFoundError as exc:
            raise GaslessSubmissionAccessError(
                "Gasless submission was not found"
            ) from exc
        except GaslessSubmissionConflictError as exc:
            raise GaslessSubmissionStateError(str(exc)) from exc
        except GaslessBlockchainError as exc:
            raise GaslessSubmissionStateError(str(exc)) from exc
        except PostgresStorageError as exc:
            raise GaslessSubmissionServiceError(str(exc)) from exc

    def status(
        self,
        submission_id: UUID,
        principal: InsurerPrincipal,
    ) -> GaslessSubmissionResponse:
        """Return credential-scoped durable progress from PostgreSQL.

        Status polling never contacts the payer wallet and never performs a
        chain write, so browser retries cannot allocate nonces or duplicate
        sponsored transactions.
        """

        try:
            record = self.store.get_for_credential(
                submission_id,
                credential_id=principal.credential_id,
            )
            return self._response(record)
        except GaslessSubmissionNotFoundError as exc:
            raise GaslessSubmissionAccessError(
                "Gasless submission was not found"
            ) from exc
        except PostgresStorageError as exc:
            raise GaslessSubmissionServiceError(str(exc)) from exc

    @staticmethod
    def _request(record: GaslessSubmissionRecord) -> PreparedForwardRequest:
        """Rehydrate typed-data fields only from the durable request record."""

        return PreparedForwardRequest.from_record(record)

    def _response(self, record: GaslessSubmissionRecord) -> GaslessSubmissionResponse:
        """Expose only the response fields valid for the record's current state.

        Typed data exists only while a request still needs a wallet signature.
        A public claim receipt exists only after all required fields have been
        confirmed on-chain. Internal raw transactions, HMAC fingerprints, and
        provider errors never cross the API boundary.
        """

        typed_data = None
        if record.state == "prepared":
            request = self._request(record)
            typed_data = EIP712TypedData.model_validate(
                request.typed_data(
                    chain_id=record.chain_id,
                    forwarder_address=record.forwarder_address,
                )
            )
        receipt = None
        if record.state == "confirmed":
            if (
                record.claim_id is None
                or record.transaction_hash is None
                or record.block_number is None
                or record.data_pointer is None
                or record.claim_hash is None
            ):
                raise GaslessSubmissionServiceError(
                    "Confirmed gasless submission has an incomplete receipt"
                )
            receipt = ClaimSubmissionResponse(
                claim_id=record.claim_id,
                transaction_hash=record.transaction_hash,
                block_number=record.block_number,
                data_pointer=record.data_pointer,
                claim_hash=record.claim_hash,
                assessment=None,
            )
        return GaslessSubmissionResponse(
            submission_id=record.submission_id,
            state=record.state,
            signer_address=record.signer_address,
            chain_id=record.chain_id,
            contract_address=record.contract_address,
            forwarder_address=record.forwarder_address,
            claim_hash=record.claim_hash,
            data_pointer=record.data_pointer,
            deadline=record.deadline,
            typed_data=typed_data,
            receipt=receipt,
            error_code=(
                record.last_error_code
                if record.state in {"failed", "expired"}
                else None
            ),
        )
