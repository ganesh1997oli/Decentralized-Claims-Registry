"""Consume submitted-claim events and independently verify their IPFS bytes.

This is the Week 5 proof that events reach Kafka. It deliberately does not run
fraud scoring yet; the durable PostgreSQL processor belongs to the next stage.
"""

from __future__ import annotations

from web3 import Web3

from integrations.ipfs import IPFSClient

from .events import (
    ClaimSubmittedEvent,
    KafkaClaimEventConsumer,
    KafkaSettings,
)


class VerifiedClaimEventHandler:
    def __init__(self, ipfs: IPFSClient):
        self.ipfs = ipfs

    def __call__(self, event: ClaimSubmittedEvent) -> None:
        payload = self.ipfs.download_pointer(event.data_pointer)
        actual_hash = Web3.keccak(payload).hex()
        expected_hash = event.claim_hash.removeprefix("0x").lower()
        if actual_hash.removeprefix("0x").lower() != expected_hash:
            raise ValueError(
                f"IPFS hash does not match for Kafka event {event.event_id}"
            )

        print(
            f"[KafkaProcessed] eventId={event.event_id} claimId={event.claim_id} "
            f"pointer={event.data_pointer} bytes={len(payload)}"
        )


def main() -> None:
    settings = KafkaSettings.from_env()
    if not settings.enabled:
        raise SystemExit("Set KAFKA_ENABLED=true before starting the consumer")

    consumer = KafkaClaimEventConsumer(settings)
    handler = VerifiedClaimEventHandler(IPFSClient.from_env())
    print(
        f"Consuming {settings.topic} from {settings.bootstrap_servers} "
        f"as group {settings.consumer_group_id}"
    )
    try:
        while True:
            consumer.process_next(handler)
    except KeyboardInterrupt:
        print("Stopping Kafka consumer")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
