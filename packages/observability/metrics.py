"""Low-cardinality metrics for the blockchain listener and scoring worker.

Metrics deliberately avoid claim IDs, transaction hashes, wallet addresses and
other per-claim labels. Those values are useful in logs, but using them as
metric labels would create an ever-growing number of time series and could make
Cloud Monitoring unnecessarily expensive.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

from .logging import get_event_logger

logger = get_event_logger(__name__)


def _metrics_port_from_env() -> int | None:
    """Return the configured metrics port, or ``None`` for local opt-out.

    Leaving ``METRICS_PORT`` empty keeps the endpoint disabled during normal
    local scripts and unit tests. The cloud Compose file sets one private,
    host-only port for each long-running process.
    """

    raw_port = os.environ.get("METRICS_PORT", "").strip()
    if not raw_port:
        return None

    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("METRICS_PORT must be a whole number") from exc

    if port not in range(1, 65_536):
        raise ValueError("METRICS_PORT must be between 1 and 65535")
    return port


def _start_private_metrics_endpoint(registry: CollectorRegistry) -> None:
    """Start the tiny HTTP endpoint when cloud monitoring is enabled."""

    port = _metrics_port_from_env()
    if port is None:
        return

    # Inside a container we listen on every container interface. Docker binds
    # this port to 127.0.0.1 on the VM, so it is not reachable from the internet.
    start_http_server(port, addr="0.0.0.0", registry=registry)
    logger.info("metrics.ready", port=port)


@dataclass
class ListenerMetrics:
    """Measurements that show whether the blockchain bridge is keeping up."""

    registry: CollectorRegistry
    latest_block: Gauge
    safe_block: Gauge
    last_processed_block: Gauge
    block_lag: Gauge
    last_success_unixtime: Gauge
    poll_errors: Counter
    events: Counter
    kafka_publications: Counter

    @classmethod
    def start_from_env(cls) -> ListenerMetrics:
        """Create the listener metrics and optionally start their HTTP server."""

        registry = CollectorRegistry()
        metrics = cls(
            registry=registry,
            latest_block=Gauge(
                "claims_listener_latest_block",
                "Newest Sepolia block observed by the listener.",
                registry=registry,
            ),
            safe_block=Gauge(
                "claims_listener_safe_block",
                "Newest block old enough to satisfy the confirmation setting.",
                registry=registry,
            ),
            last_processed_block=Gauge(
                "claims_listener_last_processed_block",
                "Last block durably processed and saved in the listener checkpoint.",
                registry=registry,
            ),
            block_lag=Gauge(
                "claims_listener_block_lag",
                "Confirmed blocks waiting for the listener to process.",
                registry=registry,
            ),
            last_success_unixtime=Gauge(
                "claims_listener_last_success_unixtime",
                "Unix time of the most recent successful listener poll.",
                registry=registry,
            ),
            poll_errors=Counter(
                "claims_listener_poll_errors_total",
                "Listener polling failures that will be retried.",
                registry=registry,
            ),
            events=Counter(
                "claims_listener_events_total",
                "Confirmed blockchain events handled by event type.",
                labelnames=("event_type",),
                registry=registry,
            ),
            kafka_publications=Counter(
                "claims_listener_kafka_publications_total",
                "Verified ClaimSubmitted events acknowledged by Kafka.",
                registry=registry,
            ),
        )
        _start_private_metrics_endpoint(registry)
        return metrics

    def observe_poll(
        self,
        *,
        latest_block: int,
        last_processed_block: int,
        confirmation_blocks: int,
    ) -> None:
        """Record one healthy poll without attaching claim-specific information."""

        safe_block = max(0, latest_block - confirmation_blocks)
        self.latest_block.set(latest_block)
        self.safe_block.set(safe_block)
        self.last_processed_block.set(last_processed_block)
        self.block_lag.set(max(0, safe_block - last_processed_block))
        self.last_success_unixtime.set(time.time())

    def observe_poll_error(self) -> None:
        """Count a recoverable RPC, IPFS, checkpoint or Kafka error."""

        self.poll_errors.inc()

    def observe_event(self, event_type: str) -> None:
        """Count a known event using a deliberately small label vocabulary."""

        self.events.labels(event_type=event_type).inc()

    def observe_kafka_publication(self) -> None:
        """Count a publication only after Kafka has acknowledged the message."""

        self.kafka_publications.inc()


@dataclass
class ScoringMetrics:
    """Measurements that explain scoring throughput, latency and failures."""

    registry: CollectorRegistry
    handled_events: Counter
    processing_seconds: Histogram
    model_inference_seconds: Histogram
    last_success_unixtime: Gauge
    last_probability: Gauge
    last_fraud_score: Gauge

    @classmethod
    def start_from_env(cls) -> ScoringMetrics:
        """Create scoring metrics and optionally expose their private endpoint."""

        registry = CollectorRegistry()
        metrics = cls(
            registry=registry,
            handled_events=Counter(
                "claims_scoring_events_total",
                "Kafka events handled by outcome.",
                labelnames=("outcome",),
                registry=registry,
            ),
            processing_seconds=Histogram(
                "claims_scoring_processing_seconds",
                "Time from starting one Kafka handler to its final outcome.",
                # These buckets show the fast path clearly while still covering
                # slow Sepolia confirmations without producing many samples.
                buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
                registry=registry,
            ),
            model_inference_seconds=Histogram(
                "claims_scoring_model_inference_seconds",
                "Time spent inside XGBoost prediction and SHAP explanation.",
                buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
                registry=registry,
            ),
            last_success_unixtime=Gauge(
                "claims_scoring_last_success_unixtime",
                "Unix time of the most recent successfully handled Kafka event.",
                registry=registry,
            ),
            last_probability=Gauge(
                "claims_scoring_last_probability",
                "Fraud probability produced for the most recently scored claim.",
                registry=registry,
            ),
            last_fraud_score=Gauge(
                "claims_scoring_last_fraud_score",
                "Latest fraud score in the smart contract's 0-to-10000 format.",
                registry=registry,
            ),
        )
        _start_private_metrics_endpoint(registry)
        return metrics

    def observe_inference(
        self,
        *,
        duration_seconds: float,
        probability: float,
        fraud_score: int,
    ) -> None:
        """Record the model result without recording any claimant identity."""

        self.model_inference_seconds.observe(duration_seconds)
        self.last_probability.set(probability)
        self.last_fraud_score.set(fraud_score)

    def observe_handled(self, *, outcome: str, duration_seconds: float) -> None:
        """Record the final result of one idempotent Kafka handler call."""

        self.handled_events.labels(outcome=outcome).inc()
        self.processing_seconds.observe(duration_seconds)
        if outcome == "completed":
            self.last_success_unixtime.set(time.time())
