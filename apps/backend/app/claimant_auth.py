"""Wallet-backed claimant sessions for public claim intake.

The module issues an EIP-4361-style challenge, exchanges its wallet signature
for a short-lived bearer session, and authenticates that session. PostgreSQL
handles one-time challenge consumption; message and token rules remain here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlparse
from uuid import UUID, uuid4

from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from packages.integrations.postgres import (
    ClaimantAuthChallengeError,
    ClaimantAuthChallengeRateLimitError,
    ClaimantAuthChallengeRecord,
)

_MINIMUM_KEY_BYTES = 32


class ClaimantAuthConfigurationError(ValueError):
    """Raised when claimant authentication is configured unsafely."""


class ClaimantAuthenticationError(PermissionError):
    """Raised when a challenge signature or bearer session is invalid."""


class ClaimantAuthenticationRateLimitError(RuntimeError):
    """Raised before issuing a challenge when stored limits are exhausted."""

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)


class ClaimantChallengeStore(Protocol):
    """Persistence interface required by claimant authentication."""

    def issue(
        self,
        record: ClaimantAuthChallengeRecord,
        *,
        client_limit_per_minute: int,
        wallet_limit_per_minute: int,
    ) -> ClaimantAuthChallengeRecord:
        """Persist a challenge after database-backed abuse-control checks."""

        ...

    def get(self, challenge_id: UUID) -> ClaimantAuthChallengeRecord | None:
        """Read the exact message that was shown to the wallet."""

        ...

    def consume(
        self,
        challenge_id: UUID,
        *,
        wallet_address: str,
        now: datetime,
    ) -> ClaimantAuthChallengeRecord:
        """Atomically consume an unexpired challenge for the recovered wallet."""

        ...


@dataclass(frozen=True)
class ClaimantSession:
    """Authenticated public-claim submitter recovered from a wallet signature."""

    subject_id: str
    claimant_address: str
    expires_at: datetime

    @property
    def credential_id(self) -> str:
        """Return the stable owner key used by the existing gasless outbox.

        The persistence column retains its historical name during the rolling
        migration from insurer credentials. Public code uses `subject_id`; this
        compatibility property keeps old relay records readable without making
        a short-lived session token part of stored ownership.
        """

        return self.subject_id

    @property
    def signer_address(self) -> str:
        """Expose the wallet address through the gasless ownership interface."""

        return self.claimant_address


@dataclass(frozen=True)
class IssuedClaimantChallenge:
    """Challenge values safe to return to an unauthenticated browser."""

    challenge_id: UUID
    message: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedClaimantSession:
    """Bearer token and public identity returned after challenge consumption."""

    access_token: str
    expires_at: datetime
    claimant_address: str


def _positive_int(
    settings: Mapping[str, str],
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    raw = settings.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ClaimantAuthConfigurationError(
            f"{name} must be a positive integer"
        ) from exc
    if value < 1 or value > maximum:
        raise ClaimantAuthConfigurationError(
            f"{name} must be between 1 and {maximum}"
        )
    return value


def _key(settings: Mapping[str, str], name: str) -> bytes:
    value = settings.get(name, "").encode("utf-8")
    if len(value) < _MINIMUM_KEY_BYTES:
        raise ClaimantAuthConfigurationError(
            f"{name} must contain at least {_MINIMUM_KEY_BYTES} bytes"
        )
    return value


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("Invalid base64url value")
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class ClaimantSessionManager:
    """Issue one-time wallet challenges and authenticate signed sessions."""

    def __init__(
        self,
        challenges: ClaimantChallengeStore,
        *,
        domain: str,
        uri: str,
        chain_id: int,
        token_key: bytes,
        subject_key: bytes,
        fingerprint_key: bytes,
        challenge_ttl_seconds: int,
        session_ttl_seconds: int,
        client_limit_per_minute: int,
        wallet_limit_per_minute: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_challenge_id: Callable[[], UUID] = uuid4,
        new_nonce: Callable[[], str] = lambda: secrets.token_hex(12),
    ) -> None:
        parsed_uri = urlparse(uri)
        if not domain or any(character.isspace() for character in domain):
            raise ClaimantAuthConfigurationError(
                "CLAIMANT_AUTH_DOMAIN must be a hostname without whitespace"
            )
        if parsed_uri.scheme not in {"http", "https"} or parsed_uri.netloc != domain:
            raise ClaimantAuthConfigurationError(
                "CLAIMANT_AUTH_URI must be HTTP(S) and match CLAIMANT_AUTH_DOMAIN"
            )
        if chain_id < 1:
            raise ClaimantAuthConfigurationError("Claimant auth chain ID must be positive")
        for name, key in (
            ("CLAIMANT_SESSION_SIGNING_KEY", token_key),
            ("CLAIMANT_SUBJECT_KEY", subject_key),
            ("CLAIMANT_AUTH_FINGERPRINT_KEY", fingerprint_key),
        ):
            if len(key) < _MINIMUM_KEY_BYTES:
                raise ClaimantAuthConfigurationError(
                    f"{name} must contain at least {_MINIMUM_KEY_BYTES} bytes"
                )

        self.challenges = challenges
        self.domain = domain
        self.uri = uri
        self.chain_id = chain_id
        self.token_key = token_key
        self.subject_key = subject_key
        self.fingerprint_key = fingerprint_key
        self.challenge_ttl_seconds = challenge_ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self.client_limit_per_minute = client_limit_per_minute
        self.wallet_limit_per_minute = wallet_limit_per_minute
        self.clock = clock
        self.new_challenge_id = new_challenge_id
        self.new_nonce = new_nonce

    @classmethod
    def from_mapping(
        cls,
        settings: Mapping[str, str],
        challenges: ClaimantChallengeStore,
        *,
        chain_id: int,
    ) -> ClaimantSessionManager:
        """Construct explicit authentication policy around an injected store."""

        return cls(
            challenges,
            domain=settings.get("CLAIMANT_AUTH_DOMAIN", "127.0.0.1:5173").strip(),
            uri=settings.get("CLAIMANT_AUTH_URI", "http://127.0.0.1:5173").strip(),
            chain_id=chain_id,
            token_key=_key(settings, "CLAIMANT_SESSION_SIGNING_KEY"),
            subject_key=_key(settings, "CLAIMANT_SUBJECT_KEY"),
            fingerprint_key=_key(settings, "CLAIMANT_AUTH_FINGERPRINT_KEY"),
            challenge_ttl_seconds=_positive_int(
                settings,
                "CLAIMANT_CHALLENGE_TTL_SECONDS",
                300,
                maximum=900,
            ),
            session_ttl_seconds=_positive_int(
                settings,
                "CLAIMANT_SESSION_TTL_SECONDS",
                900,
                maximum=3_600,
            ),
            client_limit_per_minute=_positive_int(
                settings,
                "CLAIMANT_AUTH_CLIENT_RATE_PER_MINUTE",
                20,
                maximum=1_000,
            ),
            wallet_limit_per_minute=_positive_int(
                settings,
                "CLAIMANT_AUTH_WALLET_RATE_PER_MINUTE",
                5,
                maximum=100,
            ),
        )

    @classmethod
    def from_env(
        cls,
        challenges: ClaimantChallengeStore,
        *,
        chain_id: int,
    ) -> ClaimantSessionManager:
        return cls.from_mapping(os.environ, challenges, chain_id=chain_id)

    def _fingerprint(self, namespace: str, value: str) -> str:
        return hmac.new(
            self.fingerprint_key,
            f"{namespace}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _subject_id(self, wallet_address: str) -> str:
        digest = hmac.new(
            self.subject_key,
            f"claimant:{wallet_address.lower()}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"claimant-{digest}"

    def issue_challenge(
        self,
        wallet_address: str,
        *,
        client_ip: str,
    ) -> IssuedClaimantChallenge:
        """Create a persisted EIP-4361-style proof-of-wallet challenge."""

        try:
            wallet = Web3.to_checksum_address(wallet_address)
        except ValueError as exc:
            raise ClaimantAuthenticationError("A valid claimant wallet is required") from exc
        if int(wallet, 16) == 0:
            raise ClaimantAuthenticationError("The claimant wallet cannot be zero")

        issued_at = self.clock()
        if issued_at.tzinfo is None:
            raise ClaimantAuthConfigurationError(
                "Claimant authentication clock must be timezone-aware"
            )
        issued_at = issued_at.astimezone(UTC)
        expires_at = issued_at + timedelta(seconds=self.challenge_ttl_seconds)
        challenge_id = self.new_challenge_id()
        nonce = self.new_nonce()
        if len(nonce) < 16 or not nonce.isalnum():
            raise ClaimantAuthConfigurationError(
                "Claimant challenge nonces must be at least 16 alphanumeric characters"
            )

        message = (
            f"{self.domain} wants you to sign in with your Ethereum account:\n"
            f"{wallet}\n\n"
            "Submit and track an insurance claim.\n\n"
            f"URI: {self.uri}\n"
            "Version: 1\n"
            f"Chain ID: {self.chain_id}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at.isoformat()}\n"
            f"Expiration Time: {expires_at.isoformat()}\n"
            f"Request ID: {challenge_id}"
        )
        record = ClaimantAuthChallengeRecord(
            challenge_id=challenge_id,
            wallet_address=wallet,
            nonce=nonce,
            message=message,
            client_fingerprint=self._fingerprint(
                "client", client_ip.strip() or "unknown"
            ),
            issued_at=issued_at,
            expires_at=expires_at,
        )
        try:
            saved = self.challenges.issue(
                record,
                client_limit_per_minute=self.client_limit_per_minute,
                wallet_limit_per_minute=self.wallet_limit_per_minute,
            )
        except ClaimantAuthChallengeRateLimitError as exc:
            raise ClaimantAuthenticationRateLimitError(
                str(exc), retry_after=exc.retry_after
            ) from exc
        return IssuedClaimantChallenge(
            challenge_id=saved.challenge_id,
            message=saved.message,
            expires_at=saved.expires_at,
        )

    def create_session(
        self,
        challenge_id: UUID,
        signature: str,
    ) -> IssuedClaimantSession:
        """Verify and consume a wallet challenge before issuing a bearer token."""

        challenge = self.challenges.get(challenge_id)
        now = self.clock()
        if now.tzinfo is None:
            raise ClaimantAuthConfigurationError(
                "Claimant authentication clock must be timezone-aware"
            )
        now = now.astimezone(UTC)
        if (
            challenge is None
            or challenge.consumed_at is not None
            or challenge.expires_at < now
        ):
            raise ClaimantAuthenticationError(
                "Authentication challenge is invalid, expired, or already used"
            )
        try:
            recovered = Account.recover_message(
                encode_defunct(text=challenge.message),
                signature=signature,
            )
            recovered = Web3.to_checksum_address(recovered)
        except (ValueError, TypeError) as exc:
            raise ClaimantAuthenticationError(
                "The wallet challenge signature is invalid"
            ) from exc
        if not hmac.compare_digest(
            recovered.lower(), challenge.wallet_address.lower()
        ):
            raise ClaimantAuthenticationError(
                "The wallet signature does not match the requested claimant"
            )
        try:
            consumed = self.challenges.consume(
                challenge_id,
                wallet_address=recovered,
                now=now,
            )
        except ClaimantAuthChallengeError as exc:
            raise ClaimantAuthenticationError(str(exc)) from exc

        expires_at = now + timedelta(seconds=self.session_ttl_seconds)
        payload = {
            "address": Web3.to_checksum_address(consumed.wallet_address),
            "exp": int(expires_at.timestamp()),
            "iat": int(now.timestamp()),
            "sub": self._subject_id(consumed.wallet_address),
            "version": 1,
        }
        encoded_payload = _b64encode(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature_bytes = hmac.new(
            self.token_key,
            f"v1.{encoded_payload}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        return IssuedClaimantSession(
            access_token=f"v1.{encoded_payload}.{_b64encode(signature_bytes)}",
            expires_at=expires_at,
            claimant_address=payload["address"],
        )

    def authenticate(self, access_token: str | None) -> ClaimantSession:
        """Authenticate a signed, unexpired bearer token without database I/O."""

        if not access_token:
            raise ClaimantAuthenticationError("A claimant bearer session is required")
        try:
            version, encoded_payload, encoded_signature = access_token.split(".")
            if version != "v1":
                raise ValueError("Unsupported token version")
            supplied_signature = _b64decode(encoded_signature)
            expected_signature = hmac.new(
                self.token_key,
                f"v1.{encoded_payload}".encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError("Invalid token signature")
            payload = json.loads(_b64decode(encoded_payload))
            if set(payload) != {"address", "exp", "iat", "sub", "version"}:
                raise ValueError("Invalid token fields")
            if payload["version"] != 1:
                raise ValueError("Invalid token payload version")
            address = Web3.to_checksum_address(payload["address"])
            subject_id = str(payload["sub"])
            expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
            issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ClaimantAuthenticationError(
                "The claimant session is invalid"
            ) from exc

        now = self.clock()
        if now.tzinfo is None:
            raise ClaimantAuthConfigurationError(
                "Claimant authentication clock must be timezone-aware"
            )
        now = now.astimezone(UTC)
        if expires_at <= now or issued_at > now + timedelta(seconds=30):
            raise ClaimantAuthenticationError("The claimant session has expired")
        if not subject_id.startswith("claimant-") or len(subject_id) != 73:
            raise ClaimantAuthenticationError("The claimant session subject is invalid")
        if not hmac.compare_digest(subject_id, self._subject_id(address)):
            raise ClaimantAuthenticationError("The claimant session subject is invalid")
        return ClaimantSession(
            subject_id=subject_id,
            claimant_address=address,
            expires_at=expires_at,
        )
