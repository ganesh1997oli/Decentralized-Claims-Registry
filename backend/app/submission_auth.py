"""Authenticate synthetic insurers and attest gateway-authorized claim documents.

The browser supplies an insurer API key, but the application stores only its
SHA-256 digest.  This module turns that credential into an authoritative
``InsurerPrincipal``, applies the research gateway's abuse limits, and signs the
exact claim document that the worker later verifies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from backend.app.models import ClaimSubmission, StoredClaimDocument

AUTHORIZATION_VERSION = "insurer-principal-hmac-sha256-v1"
SUBMIT_CLAIM_OPERATION = "submit_claim"
_MINIMUM_API_KEY_LENGTH = 24
_MINIMUM_AUTHORIZATION_KEY_BYTES = 32
_INSURER_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]\Z")
_CREDENTIAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


class SubmissionAuthConfigurationError(ValueError):
    """Raised when insurer authentication settings are unsafe or malformed."""


class SubmissionAuthenticationError(PermissionError):
    """Raised when no configured credential matches the supplied API key."""


class SubmissionAuthorizationError(PermissionError):
    """Raised when a credential cannot submit for the requested insurer."""


class SubmissionRateLimitError(RuntimeError):
    """Raised before external work when a submission limit has been reached."""

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)


class ClaimAuthorizationVerificationError(ValueError):
    """Raised when an IPFS claim lacks a valid gateway authorization."""


@dataclass(frozen=True)
class InsurerPrincipal:
    """The authoritative insurer identity established from one API credential."""

    insurer_id: str
    credential_id: str
    permitted_operations: frozenset[str]
    daily_quota: int


@dataclass(frozen=True)
class _Credential:
    principal: InsurerPrincipal
    api_key_sha256: str


def _positive_integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool):
        raise SubmissionAuthConfigurationError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SubmissionAuthConfigurationError(
            f"{name} must be a positive integer"
        ) from exc
    if parsed < 1:
        raise SubmissionAuthConfigurationError(f"{name} must be a positive integer")
    return parsed


def _required_text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubmissionAuthConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _parse_credentials(raw_json: str) -> tuple[_Credential, ...]:
    try:
        raw_credentials = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SubmissionAuthConfigurationError(
            "INSURER_CREDENTIALS_JSON must be valid JSON"
        ) from exc
    if not isinstance(raw_credentials, list) or not raw_credentials:
        raise SubmissionAuthConfigurationError(
            "INSURER_CREDENTIALS_JSON must contain at least one credential"
        )

    credentials: list[_Credential] = []
    credential_ids: set[str] = set()
    insurer_ids: set[str] = set()
    hashes: set[str] = set()
    for index, raw in enumerate(raw_credentials):
        if not isinstance(raw, dict):
            raise SubmissionAuthConfigurationError(
                f"Credential {index} must be a JSON object"
            )
        credential_id = _required_text(
            raw.get("credentialId"), name=f"credentialId at index {index}"
        )
        if not _CREDENTIAL_ID_PATTERN.fullmatch(credential_id):
            raise SubmissionAuthConfigurationError(
                f"credentialId at index {index} must use 1-100 letters, "
                "numbers, dots, underscores, or hyphens"
            )
        insurer_id = _required_text(
            raw.get("insurerId"), name=f"insurerId at index {index}"
        )
        if not _INSURER_ID_PATTERN.fullmatch(insurer_id):
            raise SubmissionAuthConfigurationError(
                f"insurerId at index {index} must use lowercase letters, "
                "numbers, and internal hyphens"
            )
        api_key_sha256 = _required_text(
            raw.get("apiKeySha256"), name=f"apiKeySha256 at index {index}"
        ).lower()
        if len(api_key_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in api_key_sha256
        ):
            raise SubmissionAuthConfigurationError(
                f"apiKeySha256 at index {index} must be 64 hexadecimal characters"
            )
        operations = raw.get("permittedOperations", [SUBMIT_CLAIM_OPERATION])
        if not isinstance(operations, list) or not all(
            isinstance(operation, str) and operation.strip()
            for operation in operations
        ):
            raise SubmissionAuthConfigurationError(
                f"permittedOperations at index {index} must be a list of strings"
            )
        permitted_operations = frozenset(
            operation.strip() for operation in operations
        )
        daily_quota = _positive_integer(
            raw.get("dailyQuota", 25), name=f"dailyQuota at index {index}"
        )
        if credential_id in credential_ids:
            raise SubmissionAuthConfigurationError(
                f"Duplicate credentialId {credential_id!r}"
            )
        if insurer_id in insurer_ids:
            raise SubmissionAuthConfigurationError(
                f"Duplicate insurerId {insurer_id!r}; issue one active key per insurer"
            )
        if api_key_sha256 in hashes:
            raise SubmissionAuthConfigurationError(
                "Two insurers cannot share the same API-key digest"
            )
        credential_ids.add(credential_id)
        insurer_ids.add(insurer_id)
        hashes.add(api_key_sha256)
        credentials.append(
            _Credential(
                principal=InsurerPrincipal(
                    insurer_id=insurer_id,
                    credential_id=credential_id,
                    permitted_operations=permitted_operations,
                    daily_quota=daily_quota,
                ),
                api_key_sha256=api_key_sha256,
            )
        )
    return tuple(credentials)


class SubmissionBoundary:
    """Authenticate one submission and reserve its process-local allowance."""

    def __init__(
        self,
        credentials: tuple[_Credential, ...],
        *,
        insurer_rate_limit_per_minute: int,
        ip_rate_limit_per_minute: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._credentials = credentials
        self._insurer_rate_limit = insurer_rate_limit_per_minute
        self._ip_rate_limit = ip_rate_limit_per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._ip_attempts: dict[str, deque[datetime]] = defaultdict(deque)
        self._insurer_attempts: dict[str, deque[datetime]] = defaultdict(deque)
        self._daily_usage: dict[tuple[str, date], int] = defaultdict(int)

    @classmethod
    def from_mapping(
        cls,
        settings: Mapping[str, str],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> SubmissionBoundary:
        raw_credentials = settings.get("INSURER_CREDENTIALS_JSON", "").strip()
        if not raw_credentials:
            raise SubmissionAuthConfigurationError(
                "INSURER_CREDENTIALS_JSON is required"
            )
        return cls(
            _parse_credentials(raw_credentials),
            insurer_rate_limit_per_minute=_positive_integer(
                settings.get("INSURER_RATE_LIMIT_PER_MINUTE", "5"),
                name="INSURER_RATE_LIMIT_PER_MINUTE",
            ),
            ip_rate_limit_per_minute=_positive_integer(
                settings.get("IP_RATE_LIMIT_PER_MINUTE", "20"),
                name="IP_RATE_LIMIT_PER_MINUTE",
            ),
            clock=clock,
        )

    @classmethod
    def from_env(cls) -> SubmissionBoundary:
        return cls.from_mapping(os.environ)

    @staticmethod
    def _prune(attempts: deque[datetime], cutoff: datetime) -> None:
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    def _reserve_ip_attempt(self, *, client_ip: str, now: datetime) -> None:
        cutoff = now - timedelta(minutes=1)
        attempts = self._ip_attempts[client_ip]
        self._prune(attempts, cutoff)
        if len(attempts) >= self._ip_rate_limit:
            retry_after = int((attempts[0] + timedelta(minutes=1) - now).total_seconds())
            raise SubmissionRateLimitError(
                "Too many claim-submission attempts from this IP address",
                retry_after=retry_after,
            )
        attempts.append(now)

    def _find_principal(self, api_key: str) -> InsurerPrincipal:
        if len(api_key) < _MINIMUM_API_KEY_LENGTH:
            raise SubmissionAuthenticationError("Invalid insurer API credential")
        supplied_digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        match: InsurerPrincipal | None = None
        # Compare every digest so the position of a configured insurer does not
        # create a useful timing signal.
        for credential in self._credentials:
            if hmac.compare_digest(supplied_digest, credential.api_key_sha256):
                match = credential.principal
        if match is None:
            raise SubmissionAuthenticationError("Invalid insurer API credential")
        return match

    def _reserve_principal(self, principal: InsurerPrincipal, *, now: datetime) -> None:
        cutoff = now - timedelta(minutes=1)
        attempts = self._insurer_attempts[principal.credential_id]
        self._prune(attempts, cutoff)
        if len(attempts) >= self._insurer_rate_limit:
            retry_after = int((attempts[0] + timedelta(minutes=1) - now).total_seconds())
            raise SubmissionRateLimitError(
                "This insurer has reached its per-minute submission limit",
                retry_after=retry_after,
            )

        daily_key = (principal.credential_id, now.date())
        if self._daily_usage[daily_key] >= principal.daily_quota:
            tomorrow = datetime.combine(
                now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC
            )
            raise SubmissionRateLimitError(
                "This insurer has reached its daily submission quota",
                retry_after=int((tomorrow - now).total_seconds()),
            )
        attempts.append(now)
        self._daily_usage[daily_key] += 1

        # Retain only today's counters. This single-process research gateway has
        # bounded state even if it runs continuously for many days.
        for key in tuple(self._daily_usage):
            if key[1] < now.date():
                del self._daily_usage[key]

    def authorize_and_reserve(
        self,
        *,
        api_key: str | None,
        claimed_insurer_id: str,
        client_ip: str,
    ) -> InsurerPrincipal:
        """Authenticate, authorize, and reserve capacity before external writes."""

        now = self._clock()
        if now.tzinfo is None:
            raise SubmissionAuthConfigurationError(
                "Submission clock must be timezone-aware"
            )
        now = now.astimezone(UTC)
        normalized_ip = client_ip.strip() or "unknown"
        with self._lock:
            self._reserve_ip_attempt(client_ip=normalized_ip, now=now)
            principal = self._find_principal((api_key or "").strip())
            if SUBMIT_CLAIM_OPERATION not in principal.permitted_operations:
                raise SubmissionAuthorizationError(
                    "This insurer credential cannot submit claims"
                )
            if not hmac.compare_digest(principal.insurer_id, claimed_insurer_id):
                raise SubmissionAuthorizationError(
                    "The selected insurer does not match the authenticated credential"
                )
            self._reserve_principal(principal, now=now)
        return principal


def _canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class ClaimAuthorizationSigner:
    """Sign and verify the insurer identity embedded in an IPFS claim document."""

    def __init__(self, key: bytes) -> None:
        if len(key) < _MINIMUM_AUTHORIZATION_KEY_BYTES:
            raise SubmissionAuthConfigurationError(
                "CLAIM_AUTHORIZATION_KEY must contain at least 32 bytes"
            )
        self._key = key

    @classmethod
    def from_mapping(cls, settings: Mapping[str, str]) -> ClaimAuthorizationSigner:
        raw_key = settings.get("CLAIM_AUTHORIZATION_KEY", "")
        if not raw_key:
            raise SubmissionAuthConfigurationError(
                "CLAIM_AUTHORIZATION_KEY is required"
            )
        return cls(raw_key.encode("utf-8"))

    @classmethod
    def from_env(cls) -> ClaimAuthorizationSigner:
        return cls.from_mapping(os.environ)

    @staticmethod
    def _unsigned_document(
        claim: ClaimSubmission,
        *,
        credential_id: str,
    ) -> dict[str, Any]:
        return {
            "schemaVersion": 4,
            **claim.model_dump(by_alias=True, mode="json"),
            "submissionAuthorization": {
                "version": AUTHORIZATION_VERSION,
                "credentialId": credential_id,
            },
        }

    def authorized_claim_bytes(
        self,
        claim: ClaimSubmission,
        principal: InsurerPrincipal,
    ) -> bytes:
        if not hmac.compare_digest(claim.insurer_id, principal.insurer_id):
            raise SubmissionAuthorizationError(
                "Claim insurer does not match the authenticated principal"
            )
        unsigned = self._unsigned_document(
            claim,
            credential_id=principal.credential_id,
        )
        signature = hmac.new(self._key, _canonical_json(unsigned), hashlib.sha256)
        authorized = {
            **unsigned,
            "submissionAuthorization": {
                **unsigned["submissionAuthorization"],
                "signature": signature.hexdigest(),
            },
        }
        return _canonical_json(authorized)

    def verify_claim(self, claim: StoredClaimDocument) -> InsurerPrincipal:
        authorization = claim.submission_authorization
        unsigned = self._unsigned_document(
            claim,
            credential_id=authorization.credential_id,
        )
        expected = hmac.new(
            self._key,
            _canonical_json(unsigned),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, authorization.signature):
            raise ClaimAuthorizationVerificationError(
                "Claim document was not authorized by the insurer gateway"
            )
        return InsurerPrincipal(
            insurer_id=claim.insurer_id,
            credential_id=authorization.credential_id,
            permitted_operations=frozenset({SUBMIT_CLAIM_OPERATION}),
            daily_quota=0,
        )


class ClaimRequestSizeLimitMiddleware:
    """Reject oversized claim bodies before FastAPI parses or authenticates them."""

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise SubmissionAuthConfigurationError(
                "MAX_CLAIM_BODY_BYTES must be a positive integer"
            )
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/claims"
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", ()))
        raw_content_length = headers.get(b"content-length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                await self._reject(send)
                return
            if content_length > self.max_bytes:
                await self._reject(send)
                return

        messages: list[dict[str, Any]] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                await self._reject(send)
                return
            more_body = bool(message.get("more_body", False))

        message_index = 0

        async def replay() -> dict[str, Any]:
            nonlocal message_index
            if message_index < len(messages):
                message = messages[message_index]
                message_index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, send: Any) -> None:
        body = json.dumps(
            {"detail": f"Claim request body exceeds {self.max_bytes} bytes"},
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
