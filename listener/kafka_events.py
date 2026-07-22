"""Kafka message contract and client adapters for claim events.

The rest of the application talks to the small classes in this file instead of
knowing how Kafka is configured or how messages are encoded. This keeps Kafka
details in one place and makes the listener easy to test without a live broker.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Protocol


SCHEMA_VERSION = 1
EVENT_TYPE = "ClaimSubmitted"
DEFAULT_TOPIC = "claims.submitted.v1"
SUPPORTED_SECURITY_PROTOCOLS = {
    "PLAINTEXT",
    "SSL",
    "SASL_PLAINTEXT",
    "SASL_SSL",
}


class KafkaConfigurationError(ValueError):
    """Raised when Kafka environment variables do not form a safe setup."""


class KafkaPublishError(RuntimeError):
    """Raised when Kafka does not confirm that it stored an event."""


class KafkaConsumeError(RuntimeError):
    """Raised when Kafka returns a consumer error."""


class ClaimEventDecodeError(ValueError):
    """Raised when a Kafka message does not match the supported schema."""


def _read_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise KafkaConfigurationError(f"{name} must be true or false")


def _required_text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClaimEventDecodeError(f"{name} must be a non-empty string")
    return value


def _required_int(value: Any, *, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ClaimEventDecodeError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class ClaimSubmittedEvent:
    """The stable JSON message placed on ``claims.submitted.v1``."""

    schema_version: int
    event_type: str
    event_id: str
    chain_id: int
    contract_address: str
    claim_id: int
    claimant: str
    claim_hash: str
    data_pointer: str
    block_number: int
    block_hash: str
    transaction_hash: str
    log_index: int
    event_timestamp: int

    @staticmethod
    def make_event_id(chain_id: int, transaction_hash: str, log_index: int) -> str:
        """Return the same ID every time a blockchain log is seen again."""

        return f"{chain_id}:{transaction_hash.lower()}:{log_index}"

    @classmethod
    def create(
        cls,
        *,
        chain_id: int,
        contract_address: str,
        claim_id: int,
        claimant: str,
        claim_hash: str,
        data_pointer: str,
        block_number: int,
        block_hash: str,
        transaction_hash: str,
        log_index: int,
        event_timestamp: int,
    ) -> "ClaimSubmittedEvent":
        return cls(
            schema_version=SCHEMA_VERSION,
            event_type=EVENT_TYPE,
            event_id=cls.make_event_id(chain_id, transaction_hash, log_index),
            chain_id=chain_id,
            contract_address=contract_address,
            claim_id=claim_id,
            claimant=claimant,
            claim_hash=claim_hash,
            data_pointer=data_pointer,
            block_number=block_number,
            block_hash=block_hash,
            transaction_hash=transaction_hash,
            log_index=log_index,
            event_timestamp=event_timestamp,
        )

    @property
    def partition_key(self) -> str:
        """Keep all future events for one claim on the same Kafka partition."""

        return f"{self.chain_id}:{self.contract_address.lower()}:{self.claim_id}"

    def to_json_bytes(self) -> bytes:
        """Use one compact, repeatable encoding for logs and tests."""

        return json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, value: bytes | str) -> "ClaimSubmittedEvent":
        try:
            raw = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ClaimEventDecodeError(f"Event is not valid JSON: {exc}") from exc

        if not isinstance(raw, dict):
            raise ClaimEventDecodeError("Event JSON must be an object")

        event = cls(
            schema_version=_required_int(
                raw.get("schema_version"), name="schema_version", minimum=1
            ),
            event_type=_required_text(raw.get("event_type"), name="event_type"),
            event_id=_required_text(raw.get("event_id"), name="event_id"),
            chain_id=_required_int(raw.get("chain_id"), name="chain_id", minimum=1),
            contract_address=_required_text(
                raw.get("contract_address"), name="contract_address"
            ),
            claim_id=_required_int(raw.get("claim_id"), name="claim_id"),
            claimant=_required_text(raw.get("claimant"), name="claimant"),
            claim_hash=_required_text(raw.get("claim_hash"), name="claim_hash"),
            data_pointer=_required_text(
                raw.get("data_pointer"), name="data_pointer"
            ),
            block_number=_required_int(
                raw.get("block_number"), name="block_number"
            ),
            block_hash=_required_text(raw.get("block_hash"), name="block_hash"),
            transaction_hash=_required_text(
                raw.get("transaction_hash"), name="transaction_hash"
            ),
            log_index=_required_int(raw.get("log_index"), name="log_index"),
            event_timestamp=_required_int(
                raw.get("event_timestamp"), name="event_timestamp"
            ),
        )

        if event.schema_version != SCHEMA_VERSION:
            raise ClaimEventDecodeError(
                f"Unsupported schema_version {event.schema_version}"
            )
        if event.event_type != EVENT_TYPE:
            raise ClaimEventDecodeError(f"Unsupported event_type {event.event_type!r}")

        expected_id = cls.make_event_id(
            event.chain_id, event.transaction_hash, event.log_index
        )
        if event.event_id != expected_id:
            raise ClaimEventDecodeError("event_id does not match the blockchain log")
        if not event.data_pointer.startswith("ipfs://"):
            raise ClaimEventDecodeError("data_pointer must start with ipfs://")
        return event


@dataclass(frozen=True)
class KafkaSettings:
    enabled: bool = False
    bootstrap_servers: str = "127.0.0.1:9092"
    topic: str = DEFAULT_TOPIC
    client_id: str = "claims-registry-listener"
    consumer_group_id: str = "claims-registry-verifier-v1"
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str | None = None
    sasl_username: str | None = None
    sasl_password: str | None = None
    delivery_timeout_ms: int = 30_000
    consumer_poll_seconds: float = 1.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "KafkaSettings":
        """Read Kafka settings without ever printing credentials."""

        try:
            delivery_timeout_ms = int(
                values.get("KAFKA_DELIVERY_TIMEOUT_MS", "30000")
            )
            consumer_poll_seconds = float(
                values.get("KAFKA_CONSUMER_POLL_SECONDS", "1")
            )
        except ValueError as exc:
            raise KafkaConfigurationError(
                "Kafka timeout values must be numbers"
            ) from exc

        security_protocol = values.get(
            "KAFKA_SECURITY_PROTOCOL", "PLAINTEXT"
        ).strip().upper()
        settings = cls(
            enabled=_read_bool(
                values.get("KAFKA_ENABLED", "false"), name="KAFKA_ENABLED"
            ),
            bootstrap_servers=values.get(
                "KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092"
            ).strip(),
            topic=values.get("KAFKA_CLAIM_SUBMITTED_TOPIC", DEFAULT_TOPIC).strip(),
            client_id=values.get(
                "KAFKA_CLIENT_ID", "claims-registry-listener"
            ).strip(),
            consumer_group_id=values.get(
                "KAFKA_CONSUMER_GROUP_ID", "claims-registry-verifier-v1"
            ).strip(),
            security_protocol=security_protocol,
            sasl_mechanism=values.get("KAFKA_SASL_MECHANISM") or None,
            sasl_username=values.get("KAFKA_SASL_USERNAME") or None,
            sasl_password=values.get("KAFKA_SASL_PASSWORD") or None,
            delivery_timeout_ms=delivery_timeout_ms,
            consumer_poll_seconds=consumer_poll_seconds,
        )
        settings.validate()
        return settings

    @classmethod
    def from_env(cls) -> "KafkaSettings":
        import os

        return cls.from_mapping(os.environ)

    def validate(self) -> None:
        if not self.bootstrap_servers:
            raise KafkaConfigurationError("KAFKA_BOOTSTRAP_SERVERS cannot be empty")
        if not self.topic:
            raise KafkaConfigurationError(
                "KAFKA_CLAIM_SUBMITTED_TOPIC cannot be empty"
            )
        if not self.client_id or not self.consumer_group_id:
            raise KafkaConfigurationError("Kafka client and group IDs cannot be empty")
        if self.security_protocol not in SUPPORTED_SECURITY_PROTOCOLS:
            raise KafkaConfigurationError(
                f"Unsupported KAFKA_SECURITY_PROTOCOL {self.security_protocol!r}"
            )
        if self.delivery_timeout_ms < 1_000:
            raise KafkaConfigurationError(
                "KAFKA_DELIVERY_TIMEOUT_MS must be at least 1000"
            )
        if self.consumer_poll_seconds <= 0:
            raise KafkaConfigurationError(
                "KAFKA_CONSUMER_POLL_SECONDS must be greater than zero"
            )
        if self.security_protocol.startswith("SASL"):
            if not all(
                (self.sasl_mechanism, self.sasl_username, self.sasl_password)
            ):
                raise KafkaConfigurationError(
                    "SASL Kafka requires mechanism, username and password"
                )

    def common_client_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "bootstrap.servers": self.bootstrap_servers,
            "security.protocol": self.security_protocol,
        }
        if self.security_protocol.startswith("SASL"):
            config.update(
                {
                    "sasl.mechanism": self.sasl_mechanism,
                    "sasl.username": self.sasl_username,
                    "sasl.password": self.sasl_password,
                }
            )
        return config

    def producer_config(self) -> dict[str, Any]:
        config = self.common_client_config()
        config.update(
            {
                "client.id": self.client_id,
                "enable.idempotence": True,
                "acks": "all",
                "delivery.timeout.ms": self.delivery_timeout_ms,
                "socket.keepalive.enable": True,
            }
        )
        return config

    def consumer_config(self) -> dict[str, Any]:
        config = self.common_client_config()
        config.update(
            {
                "client.id": f"{self.client_id}-consumer",
                "group.id": self.consumer_group_id,
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "auto.offset.reset": "earliest",
                "allow.auto.create.topics": False,
                "isolation.level": "read_committed",
            }
        )
        return config


class ClaimEventPublisher(Protocol):
    topic: str

    def publish(self, event: ClaimSubmittedEvent) -> None: ...

    def close(self) -> None: ...


class KafkaClaimEventPublisher:
    """Publish one event and wait until Kafka acknowledges it."""

    def __init__(self, settings: KafkaSettings, *, producer: Any | None = None):
        self.settings = settings
        self.topic = settings.topic
        if producer is None:
            try:
                from confluent_kafka import Producer
            except ImportError as exc:
                raise KafkaConfigurationError(
                    "Install listener/requirements.txt to enable Kafka"
                ) from exc
            producer = Producer(settings.producer_config())
        self._producer = producer

    def publish(self, event: ClaimSubmittedEvent) -> None:
        delivery_error: list[Any] = []
        delivered = False

        def on_delivery(error: Any, _message: Any) -> None:
            nonlocal delivered
            if error is not None:
                delivery_error.append(error)
            else:
                delivered = True

        try:
            self._producer.produce(
                self.topic,
                key=event.partition_key.encode("utf-8"),
                value=event.to_json_bytes(),
                headers=[
                    ("content-type", "application/json"),
                    ("schema-version", str(event.schema_version)),
                    ("event-id", event.event_id),
                ],
                on_delivery=on_delivery,
            )
        except Exception as exc:
            raise KafkaPublishError(
                f"Kafka rejected event {event.event_id}: {exc}"
            ) from exc

        # Waiting here is deliberate. The listener must not save its block
        # cursor until Kafka confirms that the event is durable.
        remaining = self._producer.flush(self.settings.delivery_timeout_ms / 1000)
        if remaining:
            raise KafkaPublishError(
                f"Kafka did not acknowledge event {event.event_id} before timeout"
            )
        if delivery_error:
            raise KafkaPublishError(
                f"Kafka delivery failed for event {event.event_id}: {delivery_error[0]}"
            )
        if not delivered:
            raise KafkaPublishError(
                f"Kafka returned no delivery result for event {event.event_id}"
            )

    def close(self) -> None:
        remaining = self._producer.flush(self.settings.delivery_timeout_ms / 1000)
        if remaining:
            raise KafkaPublishError(
                f"Kafka still has {remaining} event(s) waiting during shutdown"
            )


class KafkaClaimEventConsumer:
    """Read and commit events only after the application handler succeeds."""

    def __init__(self, settings: KafkaSettings, *, consumer: Any | None = None):
        self.settings = settings
        if consumer is None:
            try:
                from confluent_kafka import Consumer
            except ImportError as exc:
                raise KafkaConfigurationError(
                    "Install listener/requirements.txt to enable Kafka"
                ) from exc
            consumer = Consumer(settings.consumer_config())
        self._consumer = consumer
        self._consumer.subscribe([settings.topic])

    def process_next(
        self,
        handler: Callable[[ClaimSubmittedEvent], None],
        *,
        timeout: float | None = None,
    ) -> bool:
        message = self._consumer.poll(
            self.settings.consumer_poll_seconds if timeout is None else timeout
        )
        if message is None:
            return False
        if message.error():
            raise KafkaConsumeError(f"Kafka consumer error: {message.error()}")

        event = ClaimSubmittedEvent.from_json_bytes(message.value())
        handler(event)

        # Synchronous commit means a failed handler is replayed after restart.
        self._consumer.commit(message=message, asynchronous=False)
        return True

    def close(self) -> None:
        self._consumer.close()


def create_publisher(settings: KafkaSettings) -> ClaimEventPublisher | None:
    """Return no publisher when Kafka is intentionally disabled."""

    if not settings.enabled:
        return None
    return KafkaClaimEventPublisher(settings)
