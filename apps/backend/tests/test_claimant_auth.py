from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from apps.backend.app.claimant_auth import (
    ClaimantAuthenticationError,
    ClaimantSessionManager,
)
from packages.integrations.postgres import (
    ClaimantAuthChallengeError,
    ClaimantAuthChallengeRecord,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
CHALLENGE_ID = UUID("11111111-1111-4111-8111-111111111111")


class InMemoryChallengeStore:
    """Small compare-and-set store that preserves one-time challenge semantics."""

    def __init__(self) -> None:
        self.record: ClaimantAuthChallengeRecord | None = None

    def issue(self, record, **_limits):
        self.record = record
        return record

    def get(self, challenge_id):
        if self.record is None or self.record.challenge_id != challenge_id:
            return None
        return self.record

    def consume(self, challenge_id, *, wallet_address, now):
        record = self.get(challenge_id)
        if (
            record is None
            or record.consumed_at is not None
            or record.expires_at < now
            or record.wallet_address.lower() != wallet_address.lower()
        ):
            raise ClaimantAuthChallengeError(
                "Authentication challenge is invalid, expired, or already used"
            )
        self.record = replace(record, consumed_at=now)
        return self.record


def manager(store, *, clock=lambda: NOW):
    return ClaimantSessionManager(
        store,
        domain="claims.example.test",
        uri="https://claims.example.test",
        chain_id=11_155_111,
        token_key=b"claimant-session-signing-key-32-bytes",
        subject_key=b"claimant-stable-subject-key-32-bytes",
        fingerprint_key=b"claimant-auth-fingerprint-key-32-bytes",
        challenge_ttl_seconds=300,
        session_ttl_seconds=900,
        client_limit_per_minute=20,
        wallet_limit_per_minute=5,
        clock=clock,
        new_challenge_id=lambda: CHALLENGE_ID,
        new_nonce=lambda: "0123456789abcdef01234567",
    )


def test_wallet_challenge_creates_a_short_lived_single_use_session():
    account = Account.create("claimant-auth-test")
    store = InMemoryChallengeStore()
    subject = manager(store)

    challenge = subject.issue_challenge(account.address, client_ip="192.0.2.10")
    signature = Account.sign_message(
        encode_defunct(text=challenge.message),
        account.key,
    ).signature.hex()
    issued = subject.create_session(challenge.challenge_id, signature)
    session = subject.authenticate(issued.access_token)

    assert session.claimant_address == account.address
    assert session.subject_id.startswith("claimant-")
    assert "Submit and track an insurance claim" in challenge.message
    assert store.record is not None and store.record.consumed_at == NOW
    with pytest.raises(ClaimantAuthenticationError, match="already used"):
        subject.create_session(challenge.challenge_id, signature)


def test_wallet_challenge_rejects_a_signature_from_a_different_account():
    claimant = Account.create("claimant")
    attacker = Account.create("attacker")
    subject = manager(InMemoryChallengeStore())
    challenge = subject.issue_challenge(claimant.address, client_ip="192.0.2.10")
    attacker_signature = Account.sign_message(
        encode_defunct(text=challenge.message),
        attacker.key,
    ).signature.hex()

    with pytest.raises(ClaimantAuthenticationError, match="does not match"):
        subject.create_session(challenge.challenge_id, attacker_signature)


def test_claimant_session_expires_without_database_state():
    account = Account.create("claimant-session-expiry")
    clock = [NOW]
    subject = manager(InMemoryChallengeStore(), clock=lambda: clock[0])
    challenge = subject.issue_challenge(account.address, client_ip="192.0.2.10")
    signature = Account.sign_message(
        encode_defunct(text=challenge.message),
        account.key,
    ).signature.hex()
    issued = subject.create_session(challenge.challenge_id, signature)

    clock[0] = NOW + timedelta(minutes=16)
    with pytest.raises(ClaimantAuthenticationError, match="expired"):
        subject.authenticate(issued.access_token)
