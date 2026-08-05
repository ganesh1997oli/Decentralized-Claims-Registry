"""Authenticated, read-only operational view of the blockchain indexer.

The normal claims route deliberately performs no RPC request. This operations
service is different: an operator explicitly asks for the current chain head so
the durable PostgreSQL checkpoint can be translated into meaningful block lag.
RPC failure degrades the response instead of hiding the database facts that are
still useful during an incident.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from web3 import Web3

from apps.backend.app.models import (
    ClaimIndexEventPageResponse,
    ClaimIndexEventResponse,
    ClaimIndexReconciliationResponse,
    ClaimStatusCountsResponse,
    IndexerOperationsResponse,
)
from packages.integrations.ethereum import (
    ClaimsDeployment,
    DeploymentConfigurationError,
    load_claims_deployment,
)
from packages.integrations.postgres import (
    ClaimIndexEventPage,
    ClaimIndexEventRecord,
    ClaimIndexOperationsSnapshot,
    PostgresConfigurationError,
    PostgresRepositories,
    PostgresStorageError,
)
from packages.observability import get_event_logger

logger = get_event_logger(__name__)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
STATUS_NAMES = ("Submitted", "UnderReview", "Approved", "Rejected", "Flagged")
STATUS_VALUES = {name: index for index, name in enumerate(STATUS_NAMES)}
_EVENT_CURSOR_VERSION = 1


class IndexerOperationsConfigurationError(ValueError):
    """Raised when the protected operations surface is configured unsafely."""


class IndexerOperationsAuthenticationError(ValueError):
    """Raised when an operations API key is missing or invalid."""


class IndexerOperationsServiceError(RuntimeError):
    """Raised when an operations snapshot cannot be assembled."""


class IndexerOperationsQueryError(ValueError):
    """Raised when an event-search filter or cursor is invalid."""


class IndexerOperationsBoundary:
    """Authenticate a browser-supplied key against a stored SHA-256 digest.

    The raw key is never placed in server configuration, application logs, a
    Vite build variable, or a URL. SHA-256 is appropriate here because operator
    API keys must be high-entropy generated secrets rather than human passwords.
    Enterprise deployments should normally put this route behind their identity
    proxy as an additional perimeter control.
    """

    def __init__(self, api_key_sha256: str) -> None:
        normalized = api_key_sha256.strip().lower()
        if not _SHA256_HEX.fullmatch(normalized):
            raise IndexerOperationsConfigurationError(
                "INDEXER_OPERATIONS_API_KEY_SHA256 must contain exactly 64 "
                "lowercase hexadecimal characters"
            )
        self._api_key_sha256 = normalized

    @classmethod
    def from_env(cls) -> IndexerOperationsBoundary:
        value = os.environ.get("INDEXER_OPERATIONS_API_KEY_SHA256", "")
        if not value.strip():
            raise IndexerOperationsConfigurationError(
                "INDEXER_OPERATIONS_API_KEY_SHA256 is required"
            )
        return cls(value)

    def authenticate(self, api_key: str | None) -> None:
        if not api_key:
            raise IndexerOperationsAuthenticationError("Operations API key is required")
        candidate = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(candidate, self._api_key_sha256):
            raise IndexerOperationsAuthenticationError("Operations API key is invalid")


class ChainHeadReader(Protocol):
    def latest_block_number(self) -> int: ...


class OperationsIndexReader(Protocol):
    def get_operations_snapshot(
        self,
        *,
        chain_id: int,
        contract_address: str,
        recent_event_limit: int = 20,
    ) -> ClaimIndexOperationsSnapshot: ...

    def search_events(
        self,
        *,
        chain_id: int,
        contract_address: str,
        claim_id: int | None = None,
        transaction_hash: str | None = None,
        event_type: str | None = None,
        status: int | None = None,
        from_block: int | None = None,
        to_block: int | None = None,
        before: tuple[int, int, str] | None = None,
        limit: int = 20,
    ) -> ClaimIndexEventPage: ...


class Web3ChainHeadReader:
    """Small RPC adapter that verifies the configured chain on every sample."""

    def __init__(self, rpc_url: str, expected_chain_id: int) -> None:
        if not rpc_url.strip():
            raise IndexerOperationsConfigurationError(
                "SEPOLIA_RPC_URL is required for indexer operations"
            )
        self._web3 = Web3(Web3.HTTPProvider(rpc_url))
        self._expected_chain_id = expected_chain_id

    def latest_block_number(self) -> int:
        if not self._web3.is_connected():
            raise RuntimeError("Ethereum RPC is unavailable")
        chain_id = int(self._web3.eth.chain_id)
        if chain_id != self._expected_chain_id:
            raise RuntimeError("Ethereum RPC returned the wrong chain")
        return int(self._web3.eth.block_number)


def _non_negative_setting(settings: Mapping[str, str], name: str, default: int) -> int:
    raw_value = settings.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise IndexerOperationsConfigurationError(
            f"{name} must be a non-negative integer"
        ) from exc
    if value < 0:
        raise IndexerOperationsConfigurationError(
            f"{name} must be a non-negative integer"
        )
    return value


def _event_response(event: ClaimIndexEventRecord) -> ClaimIndexEventResponse:
    """Translate the persistence record without exposing internal fields."""

    return ClaimIndexEventResponse(
        event_id=event.event_id,
        claim_id=event.claim_id,
        event_type=event.event_type,
        block_number=event.block_number,
        transaction_hash=event.transaction_hash,
        log_index=event.log_index,
        event_timestamp=event.event_timestamp,
        status=(
            STATUS_NAMES[event.status]
            if 0 <= event.status < len(STATUS_NAMES)
            else f"Unknown({event.status})"
        ),
        fraud_score=event.fraud_score,
        indexed_at=event.indexed_at,
    )


def _encode_event_cursor(event: ClaimIndexEventRecord) -> str:
    """Encode a public chain position as an opaque URL-safe cursor."""

    payload = json.dumps(
        [
            _EVENT_CURSOR_VERSION,
            event.block_number,
            event.log_index,
            event.event_id,
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_event_cursor(cursor: str | None) -> tuple[int, int, str] | None:
    """Decode and strictly validate an untrusted browser cursor."""

    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.b64decode(
                cursor + padding,
                altchars=b"-_",
                validate=True,
            ).decode("utf-8")
        )
        if (
            not isinstance(payload, list)
            or len(payload) != 4
            or payload[0] != _EVENT_CURSOR_VERSION
            or not isinstance(payload[1], int)
            or isinstance(payload[1], bool)
            or payload[1] < 0
            or not isinstance(payload[2], int)
            or isinstance(payload[2], bool)
            or payload[2] < 0
            or not isinstance(payload[3], str)
            or not payload[3]
        ):
            raise ValueError("unexpected cursor payload")
        return payload[1], payload[2], payload[3]
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise IndexerOperationsQueryError(
            "The event cursor is invalid or has expired"
        ) from exc


class IndexerOperationsService:
    """Combine a durable DB snapshot with one current chain-head sample."""

    def __init__(
        self,
        *,
        deployment: ClaimsDeployment,
        index: OperationsIndexReader,
        chain: ChainHeadReader,
        confirmation_blocks: int = 12,
        stale_after_seconds: int = 120,
        recent_event_limit: int = 20,
    ) -> None:
        if confirmation_blocks < 0:
            raise ValueError("confirmation_blocks cannot be negative")
        if stale_after_seconds < 1:
            raise ValueError("stale_after_seconds must be at least 1")
        if not 1 <= recent_event_limit <= 100:
            raise ValueError("recent_event_limit must be between 1 and 100")
        self.deployment = deployment
        self.index = index
        self.chain = chain
        self.confirmation_blocks = confirmation_blocks
        self.stale_after_seconds = stale_after_seconds
        self.recent_event_limit = recent_event_limit

    @classmethod
    def from_env(cls) -> IndexerOperationsService:
        try:
            deployment = load_claims_deployment(os.environ)
            repositories = PostgresRepositories.from_env()
            rpc_url = (
                os.environ.get("SEPOLIA_RPC_URL") or os.environ.get("RPC_URL") or ""
            )
            return cls(
                deployment=deployment,
                index=repositories.claims,
                chain=Web3ChainHeadReader(rpc_url, deployment.chain_id),
                confirmation_blocks=_non_negative_setting(
                    os.environ, "CONFIRMATION_BLOCKS", 12
                ),
                stale_after_seconds=_non_negative_setting(
                    os.environ, "INDEXER_STALE_AFTER_SECONDS", 120
                ),
            )
        except (
            DeploymentConfigurationError,
            IndexerOperationsConfigurationError,
            PostgresConfigurationError,
            PostgresStorageError,
            ValueError,
        ) as exc:
            raise IndexerOperationsServiceError(str(exc)) from exc

    def snapshot(self) -> IndexerOperationsResponse:
        observed_at = datetime.now(UTC)
        try:
            database = self.index.get_operations_snapshot(
                chain_id=self.deployment.chain_id,
                contract_address=self.deployment.address,
                recent_event_limit=self.recent_event_limit,
            )
        except (PostgresStorageError, ValueError) as exc:
            raise IndexerOperationsServiceError(str(exc)) from exc

        latest_block: int | None = None
        try:
            latest_block = self.chain.latest_block_number()
        except Exception as exc:  # noqa: BLE001 - RPC clients vary by provider
            # Keep internal provider details out of the authenticated response as
            # well. Exception type is enough to correlate with infrastructure
            # logs without accidentally printing a credential-bearing RPC URL.
            logger.warning(
                "indexer_operations.rpc_sample_failed",
                exception_type=type(exc).__name__,
            )

        checkpoint = database.checkpoint
        indexed_through_block = (
            checkpoint.last_processed_block if checkpoint is not None else None
        )
        checkpoint_updated_at = (
            checkpoint.updated_at if checkpoint is not None else None
        )
        checkpoint_age_seconds = (
            max(0, int((observed_at - checkpoint.updated_at).total_seconds()))
            if checkpoint is not None
            else None
        )
        safe_block = (
            max(0, latest_block - self.confirmation_blocks)
            if latest_block is not None
            else None
        )
        block_lag = (
            max(0, safe_block - indexed_through_block)
            if safe_block is not None and indexed_through_block is not None
            else None
        )

        if checkpoint is None:
            state = "uninitialized"
        elif latest_block is None or indexed_through_block > latest_block:
            state = "degraded"
        elif block_lag == 0:
            state = "healthy"
        elif (
            checkpoint_age_seconds is not None
            and checkpoint_age_seconds >= self.stale_after_seconds
        ):
            state = "stalled"
        else:
            state = "catching_up"

        status_counts = database.claim_status_counts
        return IndexerOperationsResponse(
            state=state,
            rpc_status="available" if latest_block is not None else "unavailable",
            deployment_id=self.deployment.deployment_id,
            chain_id=self.deployment.chain_id,
            contract_address=self.deployment.address,
            confirmation_blocks=self.confirmation_blocks,
            stale_after_seconds=self.stale_after_seconds,
            latest_block=latest_block,
            safe_block=safe_block,
            indexed_through_block=indexed_through_block,
            block_lag=block_lag,
            checkpoint_updated_at=checkpoint_updated_at,
            checkpoint_age_seconds=checkpoint_age_seconds,
            total_claims=database.total_claims,
            total_events=database.total_events,
            submitted_events=database.submitted_events,
            assessed_events=database.assessed_events,
            claim_status_counts=ClaimStatusCountsResponse(
                submitted=status_counts[0],
                under_review=status_counts[1],
                approved=status_counts[2],
                rejected=status_counts[3],
                flagged=status_counts[4],
            ),
            recent_events=[_event_response(event) for event in database.recent_events],
            last_reconciliation=(
                ClaimIndexReconciliationResponse(
                    indexed_through_block=reconciliation.indexed_through_block,
                    chain_claims=reconciliation.chain_claims,
                    indexed_claims=reconciliation.indexed_claims,
                    missing_claim_ids=list(reconciliation.missing_claim_ids),
                    unexpected_claim_ids=list(reconciliation.unexpected_claim_ids),
                    mismatched_claim_ids=list(reconciliation.mismatched_claim_ids),
                    consistent=reconciliation.consistent,
                    duration_ms=reconciliation.duration_ms,
                    checked_at=reconciliation.checked_at,
                )
                if (reconciliation := database.last_reconciliation) is not None
                else None
            ),
            observed_at=observed_at,
        )

    def search_events(
        self,
        *,
        claim_id: int | None = None,
        transaction_hash: str | None = None,
        event_type: str | None = None,
        status: str | None = None,
        from_block: int | None = None,
        to_block: int | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ClaimIndexEventPageResponse:
        """Search immutable events without sampling RPC or changing telemetry."""

        if from_block is not None and to_block is not None and from_block > to_block:
            raise IndexerOperationsQueryError(
                "from_block cannot be greater than to_block"
            )
        status_value = None
        if status is not None:
            try:
                status_value = STATUS_VALUES[status]
            except KeyError as exc:
                raise IndexerOperationsQueryError(
                    "The requested claim status is not supported"
                ) from exc

        before = _decode_event_cursor(cursor)
        try:
            page = self.index.search_events(
                chain_id=self.deployment.chain_id,
                contract_address=self.deployment.address,
                claim_id=claim_id,
                transaction_hash=transaction_hash,
                event_type=event_type,
                status=status_value,
                from_block=from_block,
                to_block=to_block,
                before=before,
                limit=limit,
            )
        except ValueError as exc:
            raise IndexerOperationsQueryError(str(exc)) from exc
        except PostgresStorageError as exc:
            raise IndexerOperationsServiceError(str(exc)) from exc

        return ClaimIndexEventPageResponse(
            items=[_event_response(event) for event in page.events],
            page_size=limit,
            next_cursor=(
                _encode_event_cursor(page.events[-1])
                if page.has_more and page.events
                else None
            ),
        )
