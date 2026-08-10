from __future__ import annotations

import pytest
from prometheus_client import generate_latest

from packages.observability.metrics import ListenerMetrics, ScoringMetrics


def rendered_metrics(metrics: ListenerMetrics | ScoringMetrics) -> str:
    """Render one private registry exactly as the Ops Agent will scrape it."""

    return generate_latest(metrics.registry).decode("utf-8")


def test_listener_metrics_report_progress_without_claim_labels(monkeypatch):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    metrics = ListenerMetrics.start_from_env()

    metrics.observe_poll(
        latest_block=110,
        last_processed_block=106,
        confirmation_blocks=2,
    )
    metrics.observe_event("claim_submitted")
    metrics.observe_kafka_publication()
    metrics.observe_poll_error()

    output = rendered_metrics(metrics)

    assert "claims_listener_safe_block 108.0" in output
    assert "claims_listener_block_lag 2.0" in output
    assert (
        'claims_listener_events_total{event_type="claim_submitted"} 1.0' in output
    )
    assert "claims_listener_kafka_publications_total 1.0" in output
    assert "claims_listener_poll_errors_total 1.0" in output
    assert "claim_id" not in output
    assert "transaction_hash" not in output


def test_scoring_metrics_keep_model_and_total_latency_separate(monkeypatch):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    metrics = ScoringMetrics.start_from_env()

    metrics.observe_inference(
        duration_seconds=0.24,
        probability=0.71,
        fraud_score=7100,
    )
    metrics.observe_handled(outcome="completed", duration_seconds=8.5)
    metrics.observe_handled(outcome="failed", duration_seconds=1.2)
    metrics.observe_handled(outcome="quarantined", duration_seconds=0.3)

    output = rendered_metrics(metrics)

    assert "claims_scoring_model_inference_seconds_sum 0.24" in output
    assert "claims_scoring_processing_seconds_sum 10.0" in output
    assert 'claims_scoring_events_total{outcome="completed"} 1.0' in output
    assert 'claims_scoring_events_total{outcome="failed"} 1.0' in output
    assert 'claims_scoring_events_total{outcome="quarantined"} 1.0' in output
    assert "claims_scoring_last_probability 0.71" in output
    assert "claims_scoring_last_fraud_score 7100.0" in output


@pytest.mark.parametrize("value", ["not-a-port", "0", "65536"])
def test_metrics_port_rejects_unsafe_configuration(monkeypatch, value):
    monkeypatch.setenv("METRICS_PORT", value)

    with pytest.raises(ValueError, match="METRICS_PORT"):
        ListenerMetrics.start_from_env()
