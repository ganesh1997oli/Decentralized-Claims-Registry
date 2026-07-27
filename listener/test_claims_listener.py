"""Behavioral tests for confirmed blockchain-event processing."""

import json
from types import SimpleNamespace

import pytest
from web3 import Web3

from integrations.ipfs import IPFSError
from listener.claims_listener import (
    ClaimEventProcessor,
    ConfirmedBlockPoller,
    load_deployment,
)


class FakeIPFS:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.pointers: list[str] = []

    def download_pointer(self, pointer: str) -> bytes:
        self.pointers.append(pointer)
        return self.payload


class FakePublisher:
    topic = "claims.submitted.v1"

    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)


class FailingIPFS:
    def download_pointer(self, _pointer: str) -> bytes:
        raise IPFSError("gateway unavailable")


class FakeEventType:
    def __init__(self, entries) -> None:
        self.entries = entries

    def __call__(self):
        return self

    def get_logs(self, *, from_block: int, to_block: int):
        assert (from_block, to_block) == (100, 102)
        return self.entries


def submission_event(payload: bytes):
    return {
        "event": "ClaimSubmitted",
        "args": {
            "claimId": 7,
            "claimant": "0x2222222222222222222222222222222222222222",
            "claimHash": Web3.keccak(payload),
            "dataPointer": "ipfs://verified-claim",
            "timestamp": 1_750_000_000,
        },
        "blockNumber": 102,
        "blockHash": bytes.fromhex("11" * 32),
        "transactionHash": bytes.fromhex("22" * 32),
        "logIndex": 1,
    }


def assessment_event():
    return {
        "event": "ClaimAssessed",
        "args": {
            "claimId": 7,
            "newStatus": 1,
            "fraudScore": 4200,
            "assessor": "0x3333333333333333333333333333333333333333",
        },
        "blockNumber": 101,
        "transactionHash": bytes.fromhex("33" * 32),
        "logIndex": 2,
    }


def test_processor_orders_events_and_publishes_only_verified_claims(capsys):
    payload = b'{"schemaVersion":3,"claimReference":"verified"}'
    publisher = FakePublisher()
    contract = SimpleNamespace(
        events=SimpleNamespace(
            ClaimSubmitted=FakeEventType([submission_event(payload)]),
            ClaimAssessed=FakeEventType([assessment_event()]),
        )
    )
    processor = ClaimEventProcessor(
        chain_id=11_155_111,
        contract_address="0x1111111111111111111111111111111111111111",
        contract=contract,
        ipfs=FakeIPFS(payload),
        publisher=publisher,
    )

    processor.process_range(100, 102)

    output = capsys.readouterr().out
    assert output.index("[ClaimAssessed]") < output.index("[ClaimSubmitted]")
    assert len(publisher.events) == 1
    event = publisher.events[0]
    assert event.claim_id == 7
    assert event.event_id == f"11155111:0x{'22' * 32}:1"


def test_processor_rejects_tampered_ipfs_bytes_before_kafka_publish():
    committed_payload = b'{"schemaVersion":3,"claimReference":"committed"}'
    publisher = FakePublisher()
    contract = SimpleNamespace(
        events=SimpleNamespace(
            ClaimSubmitted=FakeEventType([submission_event(committed_payload)]),
            ClaimAssessed=FakeEventType([]),
        )
    )
    processor = ClaimEventProcessor(
        chain_id=11_155_111,
        contract_address="0x1111111111111111111111111111111111111111",
        contract=contract,
        ipfs=FakeIPFS(b'{"schemaVersion":3,"claimReference":"tampered"}'),
        publisher=publisher,
    )

    with pytest.raises(RuntimeError, match="IPFS verification failed"):
        processor.process_range(100, 102)

    assert publisher.events == []


def test_processor_translates_ipfs_outage_without_publishing():
    payload = b'{"schemaVersion":3,"claimReference":"committed"}'
    publisher = FakePublisher()
    contract = SimpleNamespace(
        events=SimpleNamespace(
            ClaimSubmitted=FakeEventType([submission_event(payload)]),
            ClaimAssessed=FakeEventType([]),
        )
    )
    processor = ClaimEventProcessor(
        chain_id=11_155_111,
        contract_address="0x1111111111111111111111111111111111111111",
        contract=contract,
        ipfs=FailingIPFS(),
        publisher=publisher,
    )

    with pytest.raises(RuntimeError, match="IPFS verification failed") as error:
        processor.process_range(100, 102)

    assert isinstance(error.value.__cause__, IPFSError)
    assert publisher.events == []


def test_processor_can_verify_without_kafka_when_publishing_is_disabled():
    payload = b'{"schemaVersion":3,"claimReference":"verified"}'
    contract = SimpleNamespace(
        events=SimpleNamespace(
            ClaimSubmitted=FakeEventType([submission_event(payload)]),
            ClaimAssessed=FakeEventType([]),
        )
    )
    processor = ClaimEventProcessor(
        chain_id=11_155_111,
        contract_address="0x1111111111111111111111111111111111111111",
        contract=contract,
        ipfs=FakeIPFS(payload),
        publisher=None,
    )

    processor.process_range(100, 102)


class RecordingRangeProcessor:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.ranges: list[tuple[int, int]] = []

    def process_range(self, from_block: int, to_block: int) -> None:
        self.ranges.append((from_block, to_block))
        if self.error is not None:
            raise self.error


class RecordingCheckpoint:
    def __init__(self) -> None:
        self.saved: list[int] = []

    def save(self, block_number: int) -> None:
        self.saved.append(block_number)


def test_poller_saves_only_after_every_confirmed_block_succeeds():
    processor = RecordingRangeProcessor()
    checkpoint = RecordingCheckpoint()
    poller = ConfirmedBlockPoller(
        processor=processor,
        checkpoint=checkpoint,
        confirmation_blocks=2,
    )

    last_processed = poller.process_latest(
        latest_block=105,
        last_processed=99,
    )

    assert last_processed == 103
    assert processor.ranges == [(100, 103)]
    assert checkpoint.saved == [103]


def test_poller_preserves_checkpoint_when_processing_fails():
    processor = RecordingRangeProcessor(error=RuntimeError("Kafka unavailable"))
    checkpoint = RecordingCheckpoint()
    poller = ConfirmedBlockPoller(
        processor=processor,
        checkpoint=checkpoint,
        confirmation_blocks=2,
    )

    with pytest.raises(RuntimeError, match="Kafka unavailable"):
        poller.process_latest(latest_block=105, last_processed=99)

    assert processor.ranges == [(100, 103)]
    assert checkpoint.saved == []


def test_poller_waits_for_confirmation_depth_and_rejects_invalid_configuration():
    processor = RecordingRangeProcessor()
    checkpoint = RecordingCheckpoint()
    poller = ConfirmedBlockPoller(
        processor=processor,
        checkpoint=checkpoint,
        confirmation_blocks=3,
    )

    last_processed = poller.process_latest(
        latest_block=102,
        last_processed=99,
    )

    assert last_processed == 99
    assert processor.ranges == []
    assert checkpoint.saved == []
    with pytest.raises(ValueError, match="cannot be negative"):
        ConfirmedBlockPoller(
            processor=processor,
            checkpoint=checkpoint,
            confirmation_blocks=-1,
        )


def test_load_deployment_reads_the_ignition_contract_interface(tmp_path):
    module_id = "ClaimsRegistryModule#ClaimsRegistry"
    contract_address = "0x1111111111111111111111111111111111111111"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (tmp_path / "deployed_addresses.json").write_text(
        json.dumps({module_id: contract_address}),
        encoding="utf-8",
    )
    (artifacts / f"{module_id}.json").write_text(
        json.dumps({"abi": [{"type": "event", "name": "ClaimSubmitted"}]}),
        encoding="utf-8",
    )

    address, abi = load_deployment(tmp_path, module_id)

    assert address == Web3.to_checksum_address(contract_address)
    assert abi == [{"type": "event", "name": "ClaimSubmitted"}]
