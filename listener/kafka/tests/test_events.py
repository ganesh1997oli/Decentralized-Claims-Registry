import json
import unittest

from kafka import (
    ClaimEventDecodeError,
    ClaimSubmittedEvent,
    KafkaClaimEventConsumer,
    KafkaClaimEventPublisher,
    KafkaConfigurationError,
    KafkaPublishError,
    KafkaSettings,
)


def sample_event() -> ClaimSubmittedEvent:
    return ClaimSubmittedEvent.create(
        chain_id=11155111,
        contract_address="0x1111111111111111111111111111111111111111",
        claim_id=7,
        claimant="0x2222222222222222222222222222222222222222",
        claim_hash="0xabc123",
        data_pointer="ipfs://bafy-test",
        block_number=100,
        block_hash="0xblock",
        transaction_hash="0xtransaction",
        log_index=2,
        event_timestamp=1_750_000_000,
    )


class FakeProducer:
    def __init__(self, *, delivery_error=None, remaining=0):
        self.delivery_error = delivery_error
        self.remaining = remaining
        self.call = None

    def produce(self, topic, **kwargs):
        self.call = (topic, kwargs)
        kwargs["on_delivery"](self.delivery_error, object())

    def flush(self, _timeout):
        return self.remaining


class FakeMessage:
    def __init__(self, value, *, error=None):
        self._value = value
        self._error = error

    def value(self):
        return self._value

    def error(self):
        return self._error


class FakeConsumer:
    def __init__(self, message):
        self.message = message
        self.subscriptions = None
        self.commits = []
        self.closed = False

    def subscribe(self, topics):
        self.subscriptions = topics

    def poll(self, _timeout):
        message, self.message = self.message, None
        return message

    def commit(self, *, message, asynchronous):
        self.commits.append((message, asynchronous))

    def close(self):
        self.closed = True


class ClaimEventContractTests(unittest.TestCase):
    def test_round_trip_keeps_event_id_and_fields(self):
        event = sample_event()

        decoded = ClaimSubmittedEvent.from_json_bytes(event.to_json_bytes())

        self.assertEqual(decoded, event)
        self.assertEqual(
            event.event_id, "11155111:0xtransaction:2"
        )

    def test_rejects_an_event_id_that_does_not_match_the_log(self):
        raw = json.loads(sample_event().to_json_bytes())
        raw["event_id"] = "made-up-id"

        with self.assertRaises(ClaimEventDecodeError):
            ClaimSubmittedEvent.from_json_bytes(json.dumps(raw))


class KafkaSettingsTests(unittest.TestCase):
    def test_producer_is_idempotent_and_consumer_commits_manually(self):
        settings = KafkaSettings.from_mapping({"KAFKA_ENABLED": "true"})

        self.assertTrue(settings.producer_config()["enable.idempotence"])
        self.assertEqual(settings.producer_config()["acks"], "all")
        self.assertFalse(settings.consumer_config()["enable.auto.commit"])
        self.assertFalse(settings.consumer_config()["enable.auto.offset.store"])

    def test_sasl_requires_all_credentials(self):
        with self.assertRaises(KafkaConfigurationError):
            KafkaSettings.from_mapping(
                {
                    "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
                    "KAFKA_SASL_MECHANISM": "PLAIN",
                }
            )


class KafkaPublisherTests(unittest.TestCase):
    def test_waits_for_delivery_and_uses_claim_partition_key(self):
        fake = FakeProducer()
        publisher = KafkaClaimEventPublisher(KafkaSettings(), producer=fake)
        event = sample_event()

        publisher.publish(event)

        topic, kwargs = fake.call
        self.assertEqual(topic, "claims.submitted.v1")
        self.assertEqual(kwargs["key"], event.partition_key.encode())
        self.assertEqual(
            ClaimSubmittedEvent.from_json_bytes(kwargs["value"]), event
        )

    def test_raises_when_the_broker_rejects_delivery(self):
        publisher = KafkaClaimEventPublisher(
            KafkaSettings(), producer=FakeProducer(delivery_error="broker down")
        )

        with self.assertRaises(KafkaPublishError):
            publisher.publish(sample_event())


class KafkaConsumerTests(unittest.TestCase):
    def test_commits_only_after_handler_succeeds(self):
        message = FakeMessage(sample_event().to_json_bytes())
        fake = FakeConsumer(message)
        consumer = KafkaClaimEventConsumer(KafkaSettings(), consumer=fake)
        handled = []

        processed = consumer.process_next(handled.append)

        self.assertTrue(processed)
        self.assertEqual(handled, [sample_event()])
        self.assertEqual(fake.commits, [(message, False)])

    def test_does_not_commit_when_handler_fails(self):
        message = FakeMessage(sample_event().to_json_bytes())
        fake = FakeConsumer(message)
        consumer = KafkaClaimEventConsumer(KafkaSettings(), consumer=fake)

        def fail(_event):
            raise RuntimeError("temporary IPFS failure")

        with self.assertRaises(RuntimeError):
            consumer.process_next(fail)
        self.assertEqual(fake.commits, [])


if __name__ == "__main__":
    unittest.main()
