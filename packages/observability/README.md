# Observability utilities

This package gives the long-running services consistent structured logs,
low-cardinality Prometheus metrics, and graceful shutdown behaviour.

## Quick mental model

Observability should answer **what is stuck, where, and since when** without
turning secrets or per-claim identifiers into uncontrolled monitoring data.

| File | Responsibility |
| --- | --- |
| `logging.py` | One-JSON-object-per-line logs, stable event names, JSON-safe fields and defensive secret redaction |
| `metrics.py` | Listener progress/error metrics and worker throughput/inference metrics with bounded labels |
| `shutdown.py` | Convert `SIGINT`/`SIGTERM` into a flag so loops finish active work and close clients normally |

## Logging example

```python
from packages.observability.logging import configure_logging, get_event_logger

# Configure once in the process entry point. The service value becomes a
# searchable field on every application and third-party-library log line.
configure_logging("claims-listener")
logger = get_event_logger(__name__)

# Prefer a stable event name plus fields over a sentence that operators must
# parse. Never pass secrets even though the formatter also redacts common forms.
logger.info("kafka.claim_published", event_id=event_id, claim_id=claim_id)
```

Sensitive key names, URL passwords, bearer tokens, and secret-looking query
values are replaced with `[REDACTED]`. Redaction is a safety net, not permission
to log raw credentials or full claim bodies.

## Metrics boundary

Set `METRICS_PORT` to enable the small private HTTP endpoint. Leaving it empty
disables the endpoint for ordinary local scripts and tests.

Listener metrics expose observed/safe/processed blocks, lag, last success,
poll errors, bounded event types, and acknowledged Kafka publications. Worker
metrics expose completed/failed/quarantined outcomes, total processing time,
model inference time, last success, and the most recent bounded score.

Claim IDs, wallet addresses, transaction hashes, CIDs, and event IDs belong in
logs—not Prometheus labels—because a label value creates a new time series.

## Graceful shutdown

`ShutdownSignal` installs lightweight signal handlers. The handler only sets a
thread-safe flag; the listener or worker loop notices it, completes the current
safe unit of work, and uses its existing `finally` block to close clients.

## Verify

```bash
apps/backend/.venv/bin/python -m pytest \
  packages/observability/test_logging.py \
  packages/observability/tests/test_metrics.py -q
```

The [GCP deployment guide](../../infrastructure/gcp/README.md#observability)
explains how Docker logging and the Google Ops Agent collect these signals.
