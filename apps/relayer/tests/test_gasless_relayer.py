from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from apps.backend.app.blockchain import ChainSubmission
from apps.backend.app.gasless_blockchain import GaslessBlockchainError
from apps.relayer.gasless_relayer import GaslessRelayWorker
from packages.integrations.postgres import (
    GaslessSubmissionRecord,
    SignedRelayTransaction,
)

SUBMISSION_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def authorized_record():
    return GaslessSubmissionRecord(
        submission_id=SUBMISSION_ID,
        credential_id="northstar-test-v1",
        insurer_id="northstar-mutual",
        signer_address="0x1111111111111111111111111111111111111111",
        chain_id=11_155_111,
        contract_address="0x2222222222222222222222222222222222222222",
        forwarder_address="0x3333333333333333333333333333333333333333",
        idempotency_key_hash="a" * 64,
        client_fingerprint="b" * 64,
        state="authorized",
        claim_hash="0x" + ("12" * 32),
        data_pointer="ipfs://bafy-test",
        call_data="0x1234",
        forwarder_nonce=3,
        forward_gas=250_000,
        deadline=2_000_000_000,
        insurer_signature="0x" + ("ab" * 65),
    )


class FakeStore:
    def __init__(self):
        self.record = authorized_record()
        self.errors = []

    def list_relay_candidates(self, *, limit):
        assert limit == 20
        return (self.record,)

    def persist_signed_transaction(
        self,
        submission_id,
        *,
        relayer_address,
        rpc_pending_nonce,
        sign,
    ):
        assert submission_id == SUBMISSION_ID
        assert relayer_address.endswith("44" * 20)
        assert rpc_pending_nonce == 9
        signed = sign(9)
        self.record = replace(
            self.record,
            state="signed",
            relayer_address=relayer_address,
            relayer_nonce=signed.nonce,
            raw_transaction=signed.raw_transaction,
            transaction_hash=signed.transaction_hash,
            max_fee_per_gas=signed.max_fee_per_gas,
            max_priority_fee_per_gas=signed.max_priority_fee_per_gas,
            relay_attempts=1,
        )
        return self.record

    def mark_broadcast(self, submission_id):
        assert submission_id == SUBMISSION_ID
        self.record = replace(
            self.record,
            state="broadcast",
            broadcast_at=self.record.broadcast_at or NOW,
            last_broadcast_at=NOW,
        )
        return self.record

    def list_relay_transaction_hashes(self, submission_id):
        assert submission_id == SUBMISSION_ID
        return (self.record.transaction_hash,)

    def persist_replacement_transaction(self, submission_id, *, sign):
        assert submission_id == SUBMISSION_ID
        signed = sign(
            self.record.relayer_nonce,
            self.record.max_fee_per_gas,
            self.record.max_priority_fee_per_gas,
        )
        self.record = replace(
            self.record,
            state="signed",
            raw_transaction=signed.raw_transaction,
            transaction_hash=signed.transaction_hash,
            max_fee_per_gas=signed.max_fee_per_gas,
            max_priority_fee_per_gas=signed.max_priority_fee_per_gas,
            relay_attempts=self.record.relay_attempts + 1,
        )
        return self.record

    def mark_confirmed(
        self, submission_id, *, transaction_hash, block_number, claim_id
    ):
        self.record = replace(
            self.record,
            state="confirmed",
            transaction_hash=transaction_hash,
            block_number=block_number,
            claim_id=claim_id,
        )
        return self.record

    def record_relay_error(self, submission_id, *, error_code, terminal):
        self.errors.append((submission_id, error_code, terminal))


class FakeChain:
    def __init__(self, *, error=None, receipt=True):
        self.account = SimpleNamespace(address="0x" + ("44" * 20))
        self.deployment = SimpleNamespace(chain_id=11_155_111)
        self.error = error
        self.broadcasts = []
        self.receipt_available = receipt

    def pending_nonce(self):
        return 9

    def sign_relay(
        self,
        record,
        *,
        relayer_nonce,
        minimum_max_fee_per_gas=0,
        minimum_priority_fee_per_gas=0,
    ):
        if self.error:
            raise self.error
        assert record.state in {"authorized", "broadcast"}
        return SignedRelayTransaction(
            nonce=relayer_nonce,
            raw_transaction=(
                "0xreplacement" if minimum_max_fee_per_gas else "0xraw"
            ),
            transaction_hash=("0xtx2" if minimum_max_fee_per_gas else "0xtx"),
            max_fee_per_gas=max(100, minimum_max_fee_per_gas + 20),
            max_priority_fee_per_gas=max(
                2, minimum_priority_fee_per_gas + 1
            ),
        )

    def prepare_relay_signer(
        self,
        record,
        *,
        minimum_max_fee_per_gas=0,
        minimum_priority_fee_per_gas=0,
    ):
        if self.error:
            raise self.error
        return lambda nonce: self.sign_relay(
            record,
            relayer_nonce=nonce,
            minimum_max_fee_per_gas=minimum_max_fee_per_gas,
            minimum_priority_fee_per_gas=minimum_priority_fee_per_gas,
        )

    def broadcast(self, raw_transaction, expected_hash):
        self.broadcasts.append((raw_transaction, expected_hash))
        return expected_hash

    def verify_signature(self, record, signature):
        assert signature == record.insurer_signature

    def receipt(self, transaction_hash):
        if not self.receipt_available:
            return None
        if transaction_hash not in {item[1] for item in self.broadcasts}:
            return None
        assert transaction_hash in {"0xtx", "0xtx2"}
        return {"status": 1, "blockNumber": 100, "transactionHash": b"tx"}

    def has_confirmations(self, receipt, confirmations):
        assert confirmations == 12
        return True

    def confirm(self, record, receipt):
        assert record.state == "broadcast"
        return ChainSubmission(
            claim_id=7,
            transaction_hash="0xtx",
            block_number=100,
        )


def test_worker_persists_before_broadcast_and_confirms_receipt():
    store = FakeStore()
    chain = FakeChain()
    worker = GaslessRelayWorker(
        store=store,
        chain=chain,
        confirmation_blocks=12,
        stuck_transaction_seconds=120,
        clock=lambda: NOW,
    )

    assert worker.run_once() == 1

    assert chain.broadcasts == [("0xraw", "0xtx")]
    assert store.record.state == "confirmed"
    assert store.record.claim_id == 7
    assert store.errors == []


def test_worker_keeps_fee_cap_failure_retryable():
    store = FakeStore()
    chain = FakeChain(
        error=GaslessBlockchainError(
            "Current network fees exceed the configured sponsorship cap"
        )
    )
    worker = GaslessRelayWorker(
        store=store,
        chain=chain,
        confirmation_blocks=12,
        stuck_transaction_seconds=120,
        clock=lambda: NOW,
    )

    worker.run_once()

    assert store.errors == [(SUBMISSION_ID, "fee_cap_exceeded", False)]
    assert chain.broadcasts == []


def test_worker_fee_bumps_a_stuck_transaction_with_the_same_nonce():
    store = FakeStore()
    store.record = replace(
        store.record,
        state="broadcast",
        relayer_address="0x" + ("44" * 20),
        relayer_nonce=9,
        raw_transaction="0xraw",
        transaction_hash="0xtx",
        max_fee_per_gas=100,
        max_priority_fee_per_gas=2,
        relay_attempts=1,
        broadcast_at=NOW - timedelta(minutes=5),
        last_broadcast_at=NOW - timedelta(minutes=5),
    )
    chain = FakeChain(receipt=False)
    worker = GaslessRelayWorker(
        store=store,
        chain=chain,
        confirmation_blocks=12,
        stuck_transaction_seconds=120,
        clock=lambda: NOW,
    )

    worker.run_once()

    assert store.record.state == "broadcast"
    assert store.record.relayer_nonce == 9
    assert store.record.transaction_hash == "0xtx2"
    assert store.record.relay_attempts == 2
    assert chain.broadcasts == [("0xreplacement", "0xtx2")]


def test_worker_recovers_a_receipt_after_crash_before_mark_broadcast():
    store = FakeStore()
    store.record = replace(
        store.record,
        state="signed",
        relayer_address="0x" + ("44" * 20),
        relayer_nonce=9,
        raw_transaction="0xraw",
        transaction_hash="0xtx",
        max_fee_per_gas=100,
        max_priority_fee_per_gas=2,
        relay_attempts=1,
    )
    chain = FakeChain()
    chain.broadcasts.append(("0xraw", "0xtx"))
    worker = GaslessRelayWorker(
        store=store,
        chain=chain,
        confirmation_blocks=12,
        stuck_transaction_seconds=120,
        clock=lambda: NOW,
    )

    worker.run_once()

    assert store.record.state == "confirmed"
    assert chain.broadcasts == [("0xraw", "0xtx")]
