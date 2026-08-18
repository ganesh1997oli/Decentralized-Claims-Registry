"""Verify gateway attestations and support historical insurer submissions.

Schema-v5 claim documents used digest-only insurer API credentials. Those
classes remain here so existing stored documents can still be verified during
migration. Public HTTP routes do not accept insurer credentials: they use the
wallet session and policy-eligibility boundaries in ``claimant_auth`` and
``policy_eligibility`` before issuing a schema-v6 authorization.
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

from web3 import Web3

from apps.backend.app.models import ClaimSubmission, StoredClaimDocument
from apps.backend.app.policy_eligibility import ClaimantPrincipal
from packages.observability import get_event_logger

AUTHORIZATION_VERSION = "insurer-principal-wallet-hmac-sha256-v2"
PUBLIC_AUTHORIZATION_VERSION = "claimant-policy-permit-hmac-sha256-v3"
SUBMIT_CLAIM_OPERATION = "submit_claim"
_MINIMUM_API_KEY_LENGTH = 24
_MINIMUM_AUTHORIZATION_KEY_BYTES = 32
_INSURER_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]\Z")
_CREDENTIAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")

logger = get_event_logger(__name__)


class SubmissionAuthConfigurationError(ValueError):
    """Raised when insurer authentication settings are unsafe or malformed."""


class SubmissionAuthenticationError(PermissionError):
    """Raised when no configured credential matches the supplied API key."""


class SubmissionAuthorizationError(PermissionError):
    """Raised when a credential cannot submit for the requested insurer."""


class SubmissionRateLimitError(RuntimeError):
    """Raised before external work when a submission limit has been reached."""

    def __init__(self, message: str, *, retry_after: int) -> None:
        """Clamp retry guidance to a positive HTTP-compatible delay."""

        super().__init__(message)
        self.retry_after = max(1, retry_after)


class ClaimAuthorizationVerificationError(ValueError):
    """Raised when an IPFS claim lacks a valid gateway authorization."""


@dataclass(frozen=True)
class InsurerPrincipal:
    """The authoritative insurer identity established from one API credential.

    ``rate_limit_exempt`` records credential policy only. It never activates a
    bypass by itself; the server-wide master switch must also be enabled.
    """

    insurer_id: str
    credential_id: str
    signer_address: str
    permitted_operations: frozenset[str]
    daily_quota: int
    rate_limit_exempt: bool = False


@dataclass(frozen=True)
class _Credential:
    principal: InsurerPrincipal
    api_key_sha256: str


def _positive_integer(value: Any, *, name: str) -> int:
    """Parse one positive security/quota setting without accepting booleans."""

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
    """Normalize required configuration text while rejecting empty values."""

    if not isinstance(value, str) or not value.strip():
        raise SubmissionAuthConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _boolean_setting(value: Any, *, name: str) -> bool:
    """Parse an explicit true/false environment value and reject ambiguity."""

    if not isinstance(value, str):
        raise SubmissionAuthConfigurationError(f"{name} must be true or false")
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise SubmissionAuthConfigurationError(f"{name} must be true or false")


def _parse_credentials(raw_json: str) -> tuple[_Credential, ...]:
    """Validate digest-only insurer configuration into immutable principals.

    Uniqueness checks prevent credentials, insurers, or wallet signers from
    becoming ambiguous. Raw API keys are never accepted in server configuration;
    only their lowercase SHA-256 digests are retained for comparison.
    """

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
    signer_addresses: set[str] = set()
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
        raw_signer_address = _required_text(
            raw.get("signerAddress"), name=f"signerAddress at index {index}"
        )
        try:
            signer_address = Web3.to_checksum_address(raw_signer_address)
        except ValueError as exc:
            raise SubmissionAuthConfigurationError(
                f"signerAddress at index {index} must be a valid Ethereum address"
            ) from exc
        if int(signer_address, 16) == 0:
            raise SubmissionAuthConfigurationError(
                f"signerAddress at index {index} cannot be the zero address"
            )
        operations = raw.get("permittedOperations", [SUBMIT_CLAIM_OPERATION])
        if not isinstance(operations, list) or not all(
            isinstance(operation, str) and operation.strip() for operation in operations
        ):
            raise SubmissionAuthConfigurationError(
                f"permittedOperations at index {index} must be a list of strings"
            )
        permitted_operations = frozenset(operation.strip() for operation in operations)
        daily_quota = _positive_integer(
            raw.get("dailyQuota", 25), name=f"dailyQuota at index {index}"
        )
        rate_limit_exempt = raw.get("rateLimitExempt", False)
        # Require a JSON Boolean rather than coercing strings or integers. A
        # permissive conversion would make a configuration typo capable of
        # changing a security-sensitive policy.
        if not isinstance(rate_limit_exempt, bool):
            raise SubmissionAuthConfigurationError(
                f"rateLimitExempt at index {index} must be a boolean"
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
        normalized_signer = signer_address.lower()
        if normalized_signer in signer_addresses:
            raise SubmissionAuthConfigurationError(
                "Two insurers cannot share the same Ethereum signer"
            )
        credential_ids.add(credential_id)
        insurer_ids.add(insurer_id)
        hashes.add(api_key_sha256)
        signer_addresses.add(normalized_signer)
        credentials.append(
            _Credential(
                principal=InsurerPrincipal(
                    insurer_id=insurer_id,
                    credential_id=credential_id,
                    signer_address=signer_address,
                    permitted_operations=permitted_operations,
                    daily_quota=daily_quota,
                    rate_limit_exempt=rate_limit_exempt,
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
        allow_rate_limit_bypass: bool,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Initialize the in-process authentication-abuse boundary.

        These counters are a first line of defence and protect invalid-key paths.
        PostgreSQL independently enforces durable sponsorship quotas across API
        replicas when a valid gasless preparation reaches the service layer.
        """

        self._credentials = credentials
        self._insurer_rate_limit = insurer_rate_limit_per_minute
        self._ip_rate_limit = ip_rate_limit_per_minute
        self._allow_rate_limit_bypass = allow_rate_limit_bypass
        self._clock = clock
        # These counters protect the historical schema-v5 credential path in a
        # single process. The lock makes check-and-reserve atomic locally; the
        # public intake path uses durable PostgreSQL challenge and sponsorship
        # reservations instead.
        self._lock = threading.Lock()
        self._ip_attempts: dict[str, deque[datetime]] = defaultdict(deque)
        self._insurer_attempts: dict[str, deque[datetime]] = defaultdict(deque)
        self._daily_usage: dict[tuple[str, date], int] = defaultdict(int)

    @property
    def rate_limit_bypass_enabled(self) -> bool:
        """Expose only the non-secret master-switch state for startup logging."""

        return self._allow_rate_limit_bypass

    @property
    def configured_principals(self) -> tuple[InsurerPrincipal, ...]:
        """Expose immutable historical identities for migration checks."""

        return tuple(credential.principal for credential in self._credentials)

    @classmethod
    def from_mapping(
        cls,
        settings: Mapping[str, str],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> SubmissionBoundary:
        """Build the boundary from explicit settings with strict policy parsing."""

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
            allow_rate_limit_bypass=_boolean_setting(
                settings.get("ALLOW_RATE_LIMIT_BYPASS", "false"),
                name="ALLOW_RATE_LIMIT_BYPASS",
            ),
            clock=clock,
        )

    @classmethod
    def from_env(cls) -> SubmissionBoundary:
        """Construct the authentication boundary from process configuration."""

        return cls.from_mapping(os.environ)

    @staticmethod
    def _prune(attempts: deque[datetime], cutoff: datetime) -> None:
        """Remove entries outside the rolling window, including its boundary."""

        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    def _reserve_ip_attempt(self, *, client_ip: str, now: datetime) -> None:
        """Atomically consume one source-IP attempt or report when it reopens."""

        cutoff = now - timedelta(minutes=1)
        attempts = self._ip_attempts[client_ip]
        self._prune(attempts, cutoff)
        if len(attempts) >= self._ip_rate_limit:
            retry_after = int(
                (attempts[0] + timedelta(minutes=1) - now).total_seconds()
            )
            raise SubmissionRateLimitError(
                "Too many claim-submission attempts from this IP address",
                retry_after=retry_after,
            )
        attempts.append(now)

    def _find_principal(self, api_key: str) -> InsurerPrincipal:
        """Match a raw API key against every configured digest in constant time.

        Iterating all entries avoids revealing the matching credential's list
        position through early-return timing. The returned principal contains no
        API-key material and is safe to pass into downstream authorization code.
        """

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
        """Consume minute and UTC-day capacity for an authenticated principal."""

        cutoff = now - timedelta(minutes=1)
        attempts = self._insurer_attempts[principal.credential_id]
        self._prune(attempts, cutoff)
        if len(attempts) >= self._insurer_rate_limit:
            retry_after = int(
                (attempts[0] + timedelta(minutes=1) - now).total_seconds()
            )
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
        # Mutate both counters only after both checks pass. This prevents a
        # rejected daily-quota request from consuming insurer-minute capacity.
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
        """Authenticate, authorize, and reserve capacity before external writes.

        Authentication and insurer authorisation always run. Only a valid
        principal with both bypass controls enabled skips the three counters.
        """

        now = self._clock()
        if now.tzinfo is None:
            raise SubmissionAuthConfigurationError(
                "Submission clock must be timezone-aware"
            )
        now = now.astimezone(UTC)
        normalized_ip = client_ip.strip() or "unknown"
        bypassed = False
        # Keep authentication, policy evaluation, and counter reservation in a
        # single critical section. In particular, two concurrent requests must
        # not both observe the final available slot and then both reserve it.
        with self._lock:
            try:
                principal = self._find_principal((api_key or "").strip())
            except SubmissionAuthenticationError:
                # Unknown credentials never inherit a bypass. Count their
                # attempts before returning the authentication failure so the
                # IP boundary still protects against credential guessing.
                self._reserve_ip_attempt(client_ip=normalized_ip, now=now)
                raise
            if SUBMIT_CLAIM_OPERATION not in principal.permitted_operations:
                self._reserve_ip_attempt(client_ip=normalized_ip, now=now)
                raise SubmissionAuthorizationError(
                    "This insurer credential cannot submit claims"
                )
            if not hmac.compare_digest(principal.insurer_id, claimed_insurer_id):
                self._reserve_ip_attempt(client_ip=normalized_ip, now=now)
                raise SubmissionAuthorizationError(
                    "The selected insurer does not match the authenticated credential"
                )

            # Both controls must opt in: marking a credential as exempt is
            # harmless while the server-wide switch remains false. This
            # fail-closed pairing prevents an accidentally copied credential
            # record from silently disabling normal abuse protection.
            bypassed = self._allow_rate_limit_bypass and principal.rate_limit_exempt
            if not bypassed:
                self._reserve_ip_attempt(client_ip=normalized_ip, now=now)
                self._reserve_principal(principal, now=now)

        if bypassed:
            # Logging happens after releasing the counter lock so log-handler
            # latency cannot serialize otherwise independent submissions.
            # Record only non-secret identifiers. The raw API key and its digest
            # must never enter logs, even for an authorised performance test.
            logger.warning(
                "submission.rate_limit_bypassed",
                insurer_id=principal.insurer_id,
                bypass_scope="ip,insurer_minute,daily_quota",
            )
        return principal

    def authenticate(
        self,
        *,
        api_key: str | None,
        client_ip: str,
    ) -> InsurerPrincipal:
        """Authenticate a follow-up operation without consuming claim quota.

        Preparing a claim is the only operation that reserves sponsorship
        capacity. Signature authorization and status polling still require the
        same insurer credential, but retries must remain idempotent and free.
        Invalid credentials continue to consume the process-local IP attempt
        limit as a first line of defence in front of the durable sponsor quota.
        """

        now = self._clock()
        if now.tzinfo is None:
            raise SubmissionAuthConfigurationError(
                "Submission clock must be timezone-aware"
            )
        now = now.astimezone(UTC)
        normalized_ip = client_ip.strip() or "unknown"
        with self._lock:
            try:
                principal = self._find_principal((api_key or "").strip())
            except SubmissionAuthenticationError:
                self._reserve_ip_attempt(client_ip=normalized_ip, now=now)
                raise
            if SUBMIT_CLAIM_OPERATION not in principal.permitted_operations:
                self._reserve_ip_attempt(client_ip=normalized_ip, now=now)
                raise SubmissionAuthorizationError(
                    "This insurer credential cannot submit claims"
                )
        return principal


def _canonical_json(document: Mapping[str, Any]) -> bytes:
    """Serialize an authorization document to deterministic UTF-8 bytes."""

    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class ClaimAuthorizationSigner:
    """Attest the parties and eligibility embedded in an IPFS claim document."""

    def __init__(self, key: bytes) -> None:
        """Require enough HMAC key material before signing canonical claim bytes."""

        if len(key) < _MINIMUM_AUTHORIZATION_KEY_BYTES:
            raise SubmissionAuthConfigurationError(
                "CLAIM_AUTHORIZATION_KEY must contain at least 32 bytes"
            )
        self._key = key

    @classmethod
    def from_mapping(cls, settings: Mapping[str, str]) -> ClaimAuthorizationSigner:
        """Load the API/worker shared authorization key from explicit settings."""

        raw_key = settings.get("CLAIM_AUTHORIZATION_KEY", "")
        if not raw_key:
            raise SubmissionAuthConfigurationError(
                "CLAIM_AUTHORIZATION_KEY is required"
            )
        return cls(raw_key.encode("utf-8"))

    @classmethod
    def from_env(cls) -> ClaimAuthorizationSigner:
        """Construct the claim authorization signer from the environment."""

        return cls.from_mapping(os.environ)

    @staticmethod
    def _unsigned_document(
        claim: ClaimSubmission,
        *,
        credential_id: str,
        signer_address: str,
    ) -> dict[str, Any]:
        """Build the exact schema-v5 document covered by the gateway HMAC."""

        return {
            "schemaVersion": 5,
            **claim.model_dump(by_alias=True, mode="json"),
            "submissionAuthorization": {
                "version": AUTHORIZATION_VERSION,
                "credentialId": credential_id,
                "signerAddress": signer_address,
            },
        }

    @staticmethod
    def _unsigned_public_document(
        claim: ClaimSubmission,
        principal: ClaimantPrincipal,
    ) -> dict[str, Any]:
        """Build the schema-v6 document for a policy-eligible public claimant."""

        return {
            "schemaVersion": 6,
            **claim.model_dump(by_alias=True, mode="json"),
            "submissionAuthorization": {
                "version": PUBLIC_AUTHORIZATION_VERSION,
                "subjectId": principal.subject_id,
                "signerAddress": principal.submitter_address,
                "claimantAddress": principal.claimant_address,
                "claimantCommitment": principal.claimant_commitment,
                "insurerId": principal.insurer_id,
                "insurerAddress": principal.insurer_address,
                "policyId": principal.policy_id,
            },
        }

    def authorized_claim_bytes(
        self,
        claim: ClaimSubmission,
        principal: InsurerPrincipal | ClaimantPrincipal,
    ) -> bytes:
        """Bind the verified claim parties to canonical claim bytes.

        The HMAC is an internal API-to-worker attestation, not the EIP-712 wallet
        authorization. The worker verifies both this identity binding and the
        public on-chain claimant before it trusts claim fields for scoring.
        """

        if not hmac.compare_digest(claim.insurer_id, principal.insurer_id):
            raise SubmissionAuthorizationError(
                "Claim insurer does not match the authenticated principal"
            )
        if isinstance(principal, ClaimantPrincipal):
            unsigned = self._unsigned_public_document(claim, principal)
        else:
            unsigned = self._unsigned_document(
                claim,
                credential_id=principal.credential_id,
                signer_address=principal.signer_address,
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

    def verify_claim(
        self,
        claim: StoredClaimDocument,
    ) -> InsurerPrincipal | ClaimantPrincipal:
        """Verify the gateway HMAC and recover its versioned claim parties."""

        authorization = claim.submission_authorization
        if authorization.version == PUBLIC_AUTHORIZATION_VERSION:
            assert authorization.subject_id is not None
            assert authorization.claimant_address is not None
            assert authorization.claimant_commitment is not None
            assert authorization.insurer_id is not None
            assert authorization.insurer_address is not None
            assert authorization.policy_id is not None
            principal: InsurerPrincipal | ClaimantPrincipal = ClaimantPrincipal(
                subject_id=authorization.subject_id,
                claimant_address=Web3.to_checksum_address(
                    authorization.claimant_address
                ),
                submitter_address=Web3.to_checksum_address(
                    authorization.signer_address
                ),
                claimant_commitment=authorization.claimant_commitment,
                insurer_id=authorization.insurer_id,
                insurer_address=Web3.to_checksum_address(
                    authorization.insurer_address
                ),
                policy_id=authorization.policy_id,
                daily_quota=0,
            )
            unsigned = self._unsigned_public_document(claim, principal)
        else:
            assert authorization.credential_id is not None
            principal = InsurerPrincipal(
                insurer_id=claim.insurer_id,
                credential_id=authorization.credential_id,
                signer_address=Web3.to_checksum_address(
                    authorization.signer_address
                ),
                permitted_operations=frozenset({SUBMIT_CLAIM_OPERATION}),
                daily_quota=0,
            )
            unsigned = self._unsigned_document(
                claim,
                credential_id=authorization.credential_id,
                signer_address=authorization.signer_address,
            )
        expected = hmac.new(
            self._key,
            _canonical_json(unsigned),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, authorization.signature):
            raise ClaimAuthorizationVerificationError(
                "Claim document was not authorized by the claims gateway"
            )
        if not hmac.compare_digest(principal.insurer_id, claim.insurer_id):
            raise ClaimAuthorizationVerificationError(
                "Authorized insurer identity does not match the claim"
            )
        return principal


class ClaimRequestSizeLimitMiddleware:
    """Reject oversized claim bodies before FastAPI parses or authenticates them."""

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        """Configure a strict pre-parser byte limit for claim preparation bodies."""

        if max_bytes < 1:
            raise SubmissionAuthConfigurationError(
                "MAX_CLAIM_BODY_BYTES must be a positive integer"
            )
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Buffer one claim body up to the cap, then replay it to FastAPI.

        Both declared and streamed sizes are checked so a client cannot bypass
        the limit by omitting or falsifying ``Content-Length``. Non-claim routes
        pass through without buffering.
        """

        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") in {"/claims", "/claims/gasless/prepare"}
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
            """Feed the validated ASGI body messages to the downstream parser."""

            nonlocal message_index
            if message_index < len(messages):
                message = messages[message_index]
                message_index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, send: Any) -> None:
        """Emit a small deterministic 413 response without parsing the body."""

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
