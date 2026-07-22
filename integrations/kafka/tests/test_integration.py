"""Optional smoke test against the local Docker Kafka broker."""

import os
import time
import unittest
import uuid

from integrations.kafka import (
    ClaimSubmittedEvent,
    KafkaClaimEventConsumer,
    KafkaClaimEventPublisher,
    KafkaSettings,
)


@unittest.skipUnless(
    os.environ.get("KAFKA_INTEGRATION_TEST") == "true",
    "set KAFKA_INTEGRATION_TEST=true to test a live broker",
)
class LiveKafkaTests(unittest.TestCase):
    def test_publishes_and_consumes_the_versioned_event(self):
        unique_id = uuid.uuid4().hex
        settings = KafkaSettings.from_mapping(
            {
                "KAFKA_ENABLED": "true",
                "KAFKA_BOOTSTRAP_SERVERS": os.environ.get(
                    "KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092"
                ),
                "KAFKA_CONSUMER_GROUP_ID": f"claims-smoke-{unique_id}",
            }
        )
        event = ClaimSubmittedEvent.create(
            chain_id=11155111,
            contract_address="0x1111111111111111111111111111111111111111",
            claim_id=1,
            claimant="0x2222222222222222222222222222222222222222",
            claim_hash="0xabc123",
            data_pointer="ipfs://bafy-smoke-test",
            block_number=1,
            block_hash="0xblock",
            transaction_hash=f"0x{unique_id}",
            log_index=0,
            event_timestamp=1_750_000_000,
        )

        publisher = KafkaClaimEventPublisher(settings)
        consumer = KafkaClaimEventConsumer(settings)
        received = []
        try:
            publisher.publish(event)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and event not in received:
                consumer.process_next(received.append, timeout=1)
        finally:
            publisher.close()
            consumer.close()

        self.assertIn(event, received)


if __name__ == "__main__":
    unittest.main()
