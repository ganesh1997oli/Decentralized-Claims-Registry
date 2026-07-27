"""Tests for the verification-only Kafka consumer handler."""

import pytest
from web3 import Web3

from integrations.kafka import ClaimSubmittedEvent
from integrations.kafka.consumer import VerifiedClaimEventHandler


class PayloadReader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def download_pointer(self, pointer: str) -> bytes:
        assert pointer == "ipfs://verified-claim"
        return self.payload


def event(payload: bytes) -> ClaimSubmittedEvent:
    return ClaimSubmittedEvent.create(
        chain_id=11_155_111,
        contract_address="0x1111111111111111111111111111111111111111",
        claim_id=7,
        claimant="0x2222222222222222222222222222222222222222",
        claim_hash=Web3.keccak(payload).hex(),
        data_pointer="ipfs://verified-claim",
        block_number=100,
        block_hash="0xblock",
        transaction_hash="0xtransaction",
        log_index=0,
        event_timestamp=1_750_000_000,
    )


def test_handler_accepts_only_bytes_committed_by_the_claim_event(capsys):
    payload = b'{"schemaVersion":3,"claimReference":"verified"}'
    handler = VerifiedClaimEventHandler(PayloadReader(payload))

    handler(event(payload))

    output = capsys.readouterr().out
    assert "[KafkaProcessed]" in output
    assert "claimId=7" in output


def test_handler_rejects_payload_that_differs_from_the_claim_event():
    committed = b'{"schemaVersion":3,"claimReference":"committed"}'
    handler = VerifiedClaimEventHandler(
        PayloadReader(b'{"schemaVersion":3,"claimReference":"tampered"}')
    )

    with pytest.raises(ValueError, match="IPFS hash does not match"):
        handler(event(committed))
