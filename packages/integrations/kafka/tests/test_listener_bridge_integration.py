"""Integration evidence for the blockchain-listener-to-Kafka boundary.

The unit tests exercise the listener and Kafka adapter separately.  This test
joins those public interfaces together and uses a real Kafka broker, proving
that a confirmed ``ClaimSubmitted`` log can cross the actual Week 5 bridge.

Sepolia and IPFS remain deterministic test boundaries here.  Depending on a
public testnet or gateway would make the automated suite slow and unreliable;
the behaviour under test is the repository-owned listener and Kafka transport.
"""

import time
from types import SimpleNamespace

import pytest
from web3 import Web3

from apps.listener.claims_listener import ClaimEventProcessor
from packages.integrations.kafka import (
    KafkaClaimEventConsumer,
    KafkaClaimEventPublisher,
)

pytestmark = pytest.mark.integration

CHAIN_ID = 11_155_111
CONTRACT_ADDRESS = "0x1111111111111111111111111111111111111111"
CLAIMANT = "0x2222222222222222222222222222222222222222"
CLAIM_POINTER = "ipfs://listener-bridge-claim"
BLOCK_NUMBER = 102
LOG_INDEX = 3
EVENT_TIMESTAMP = 1_750_000_000
# Known Keccak-256 result for the exact payload used by the test.  Keeping this
# as an independent literal means the assertion cannot pass merely because it
# recalculates the expected value with the same operation as the listener.
EXPECTED_CLAIM_HASH = (
    "0x5ef5d1cfce24be2ef724325c3b9e83b309e287e70128a3ddca6e15ec3de631d4"
)


class StaticEventQuery:
    """Present deterministic logs through the small Web3 event-query interface."""

    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries

    def __call__(self):
        # Web3 exposes events as ``contract.events.EventName()``.  Returning this
        # object keeps the fake at that external boundary without replacing any
        # listener behaviour.
        return self

    def get_logs(self, *, from_block: int, to_block: int) -> list[dict]:
        # Send the same confirmed range used by the
        # listener below.  An unexpected range would make the fixture misleading.
        assert (from_block, to_block) == (100, BLOCK_NUMBER)
        return self.entries


class StaticPayloadReader:
    """Represent IPFS while preserving the listener's public download interface."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def download_pointer(self, pointer: str, *, attempts: int = 3) -> bytes:
        assert pointer == CLAIM_POINTER
        assert attempts == 3
        return self.payload


def test_confirmed_claim_submission_is_verified_and_delivered_to_kafka(
    kafka_settings,
):
    """A verified blockchain log is observable through the real Kafka consumer."""

    payload = b'{"claimReference":"week-5-listener-bridge"}'
    claim_hash = Web3.keccak(payload)
    transaction_hash = bytes.fromhex("22" * 32)
    block_hash = bytes.fromhex("11" * 32)
    submission_log = {
        "event": "ClaimSubmitted",
        "args": {
            "claimId": 7,
            "claimant": CLAIMANT,
            "claimHash": claim_hash,
            "dataPointer": CLAIM_POINTER,
            "timestamp": EVENT_TIMESTAMP,
        },
        "blockNumber": BLOCK_NUMBER,
        "blockHash": block_hash,
        "transactionHash": transaction_hash,
        "logIndex": LOG_INDEX,
    }

    # ClaimEventProcessor asks for both lifecycle event types.  Only a submitted
    # event is present in this range, which is the event Kafka is designed to
    # carry to the scoring workflow.
    contract = SimpleNamespace(
        events=SimpleNamespace(
            ClaimSubmitted=StaticEventQuery([submission_log]),
            ClaimAssessed=StaticEventQuery([]),
        )
    )
    publisher = KafkaClaimEventPublisher(kafka_settings)
    consumer = KafkaClaimEventConsumer(kafka_settings)
    received = []
    processor = ClaimEventProcessor(
        chain_id=CHAIN_ID,
        contract_address=CONTRACT_ADDRESS,
        contract=contract,
        ipfs=StaticPayloadReader(payload),
        publisher=publisher,
    )

    try:
        # This is the public listener operation used after the poller has selected
        # a confirmed block range.  It verifies the IPFS bytes before publishing.
        processor.process_range(100, BLOCK_NUMBER)

        # Consumer-group assignment can take a moment on a newly created topic,
        # so poll within a bounded deadline instead of relying on an arbitrary
        # sleep that would be either slow or flaky.
        deadline = time.monotonic() + 15
        while not received and time.monotonic() < deadline:
            consumer.process_next(received.append, timeout=1)
    finally:
        publisher.close()
        consumer.close()

    assert len(received) == 1
    event = received[0]
    assert event.schema_version == 1
    assert event.event_type == "ClaimSubmitted"
    assert event.event_id == f"{CHAIN_ID}:0x{'22' * 32}:{LOG_INDEX}"
    assert event.chain_id == CHAIN_ID
    assert event.contract_address == CONTRACT_ADDRESS
    assert event.claim_id == 7
    assert event.claimant == CLAIMANT
    assert event.claim_hash == EXPECTED_CLAIM_HASH
    assert event.data_pointer == CLAIM_POINTER
    assert event.block_number == BLOCK_NUMBER
    assert event.block_hash == f"0x{'11' * 32}"
    assert event.transaction_hash == f"0x{'22' * 32}"
    assert event.log_index == LOG_INDEX
    assert event.event_timestamp == EVENT_TIMESTAMP
