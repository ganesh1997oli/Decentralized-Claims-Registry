"""PostgreSQL integration tests for the sponsored-transaction outbox."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from packages.integrations.postgres import (
    GaslessSubmissionConflictError,
    SignedRelayTransaction,
)

pytestmark = pytest.mark.integration

SIGNER = "0x1111111111111111111111111111111111111111"
CONTRACT = "0x2222222222222222222222222222222222222222"
FORWARDER = "0x3333333333333333333333333333333333333333"
RELAYER = "0x4444444444444444444444444444444444444444"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def preparation_values(submission_id, *, idempotency="a" * 64):
    return {
        "submission_id": submission_id,
        "credential_id": "northstar-test-v1",
        "insurer_id": "northstar-mutual",
        "signer_address": SIGNER,
        "chain_id": 11_155_111,
        "contract_address": CONTRACT,
        "forwarder_address": FORWARDER,
        "idempotency_key_hash": idempotency,
        "request_fingerprint": "b" * 64,
        "client_fingerprint": "c" * 64,
        "insurer_minute_limit": 5,
        "client_minute_limit": 20,
        "daily_quota": 25,
        "bypass_limits": False,
        "now": NOW,
    }


def test_gasless_outbox_is_idempotent_and_retains_replacement_hashes(
    postgres_repositories,
):
    store = postgres_repositories.gasless_submissions
    submission_id = uuid4()
    values = preparation_values(submission_id)

    preparing, created = store.begin_preparation(**values)
    repeated, repeated_created = store.begin_preparation(**values)

    assert created is True
    assert repeated_created is False
    assert repeated.submission_id == preparing.submission_id

    prepared = store.mark_prepared(
        submission_id,
        claim_hash="0x" + ("12" * 32),
        data_pointer="ipfs://bafy-test",
        call_data="0x1234",
        forwarder_nonce=7,
        forward_gas=250_000,
        deadline=2_000_000_000,
    )
    authorized = store.authorize(
        submission_id,
        credential_id=values["credential_id"],
        signature="0x" + ("ab" * 65),
        now=NOW,
    )
    assert prepared.state == "prepared"
    assert authorized.state == "authorized"

    signed = store.persist_signed_transaction(
        submission_id,
        relayer_address=RELAYER,
        rpc_pending_nonce=9,
        sign=lambda nonce: SignedRelayTransaction(
            nonce=nonce,
            raw_transaction="0x01",
            transaction_hash="0x" + ("34" * 32),
            max_fee_per_gas=100,
            max_priority_fee_per_gas=2,
        ),
    )
    broadcast = store.mark_broadcast(submission_id)
    replacement = store.persist_replacement_transaction(
        submission_id,
        sign=lambda nonce, _max_fee, _priority: SignedRelayTransaction(
            nonce=nonce,
            raw_transaction="0x02",
            transaction_hash="0x" + ("56" * 32),
            max_fee_per_gas=120,
            max_priority_fee_per_gas=3,
        ),
    )

    assert signed.relayer_nonce == 9
    assert broadcast.state == "broadcast"
    assert replacement.state == "signed"
    assert replacement.relayer_nonce == 9
    assert replacement.relay_attempts == 2
    assert store.list_relay_transaction_hashes(submission_id) == (
        "0x" + ("56" * 32),
        "0x" + ("34" * 32),
    )


def test_idempotency_key_is_bound_to_one_claim_fingerprint(
    postgres_repositories,
):
    store = postgres_repositories.gasless_submissions
    values = preparation_values(uuid4())
    store.begin_preparation(**values)

    with pytest.raises(GaslessSubmissionConflictError, match="different claim"):
        store.begin_preparation(
            **{**values, "submission_id": uuid4(), "request_fingerprint": "d" * 64}
        )
