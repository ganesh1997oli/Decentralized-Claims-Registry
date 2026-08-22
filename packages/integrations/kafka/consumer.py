"""Consume submitted-claim events and independently verify their IPFS bytes.

This diagnostic proves that events reach Kafka and that their IPFS bytes still
match the on-chain hash. It stops before fraud scoring; ``scoring_worker.py``
handles the full PostgreSQL-backed workflow.
"""

from __future__ import annotations

from web3 import Web3

from packages.integrations.ipfs import IPFSClient
from packages.observability import configure_logging, get_event_logger

from .events import (
    ClaimSubmittedEvent,
    KafkaClaimEventConsumer,
    KafkaSettings,
)

logger = get_event_logger(__name__)


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

        logger.info(
            "kafka.claim_processed",
            event_id=event.event_id,
            claim_id=event.claim_id,
            data_pointer=event.data_pointer,
            payload_bytes=len(payload),
        )


def main() -> None:
    configure_logging("claims-verification-consumer")
    settings = KafkaSettings.from_env()
    if not settings.enabled:
        raise SystemExit("Set KAFKA_ENABLED=true before starting the consumer")

    consumer = KafkaClaimEventConsumer(settings)
    handler = VerifiedClaimEventHandler(IPFSClient.from_env())
    logger.info(
        "consumer.started",
        topic=settings.topic,
        bootstrap_servers=settings.bootstrap_servers,
        consumer_group_id=settings.consumer_group_id,
    )
    try:
        while True:
            consumer.process_next(handler)
    except KeyboardInterrupt:
        logger.info("consumer.stopping", reason="keyboard_interrupt")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
