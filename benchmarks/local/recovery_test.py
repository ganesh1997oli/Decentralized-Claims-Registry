"""Measure Kafka replay recovery after a side effect but before offset commit."""

from __future__ import annotations

import argparse
import csv
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from confluent_kafka import Consumer

from apps.backend.app.submission_auth import ClaimAuthorizationSigner
from benchmarks.local.adapters import (
    BENCHMARK_AUTHORIZATION_KEY,
    BENCHMARK_FINGERPRINT_KEY,
    BenchmarkAssessmentRegistry,
    BenchmarkPayloadStore,
    DisposableRepositories,
    TimingScorer,
    repository_manifest,
    write_json,
)
from benchmarks.local.pipeline_load import (
    DEFAULT_DATABASE_URL,
    DEFAULT_KAFKA,
    _create_topic,
    _delete_topic,
    _settings,
    claim_payload,
    submitted_event,
)
from packages.duplicates import CrossInsurerDuplicateDetector
from packages.integrations.kafka import (
    ClaimSubmittedEvent,
    KafkaClaimEventConsumer,
    KafkaClaimEventPublisher,
)
from packages.integrations.kafka.scoring_worker import ClaimScoringHandler
from packages.integrations.postgres import ClaimFeatureProcessor
from packages.model.xgboost_scorer import XGBoostFraudScorer


@dataclass(frozen=True)
class RecoveryPoint:
    run_id: str
    elapsed_after_restart_ms: float
    delivered_after_restart: int
    unique_completed: int
    application_backlog: int


@dataclass(frozen=True)
class RecoverySummary:
    run_id: str
    backlog: int
    committed_before_crash: int
    crash_event_id: str
    redelivered_crash_event: bool
    delivered_after_restart: int
    unique_completed: int
    assessment_writes: int
    duplicate_assessment_writes: int
    lost_claims: int
    recovery_time_ms: float


def _next_message(consumer: Consumer, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = consumer.poll(0.5)
        if message is None:
            continue
        if message.error():
            raise RuntimeError(f"Kafka consumer error: {message.error()}")
        return message
    raise TimeoutError("Kafka did not deliver the crash-window event")


def _write_csv(path: Path, rows: list[object]) -> None:
    if not rows:
        return
    values = [asdict(row) for row in rows]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(values[0]))
        writer.writeheader()
        writer.writerows(values)


def run_recovery(
    *,
    run_id: str,
    backlog: int,
    crash_after: int,
    timeout: float,
    database_url: str,
    kafka_bootstrap_servers: str,
    keep_schema: bool,
) -> tuple[list[RecoveryPoint], RecoverySummary]:
    scenario_id = f"{run_id}-{uuid4().hex[:8]}"
    settings = _settings(kafka_bootstrap_servers, scenario_id)
    admin = _create_topic(settings)
    authorization = ClaimAuthorizationSigner(BENCHMARK_AUTHORIZATION_KEY)
    payload_store = BenchmarkPayloadStore()
    registry = BenchmarkAssessmentRegistry()

    try:
        with DisposableRepositories(
            database_url,
            scenario_id,
            keep_schema=keep_schema,
        ) as repositories:
            scorer = TimingScorer(XGBoostFraudScorer.from_env())
            handler = ClaimScoringHandler(
                ipfs=payload_store,
                scorer=scorer,
                duplicate_detector=CrossInsurerDuplicateDetector(
                    BENCHMARK_FINGERPRINT_KEY,
                    repositories.duplicates,
                ),
                feature_processor=ClaimFeatureProcessor(
                    BENCHMARK_FINGERPRINT_KEY,
                    repositories.features,
                ),
                repository=repositories.assessments,
                registry=registry,
                authorization=authorization,
            )
            events = []
            publisher = KafkaClaimEventPublisher(settings)
            try:
                for claim_id in range(1, backlog + 1):
                    payload = claim_payload(claim_id, authorization)
                    event = submitted_event(claim_id, payload)
                    payload_store.put_pointer(event.data_pointer, payload)
                    publisher.publish(event)
                    events.append(event)
            finally:
                publisher.close()

            committed_consumer = KafkaClaimEventConsumer(settings)
            committed_event_ids: list[str] = []

            def committed_handler(event: ClaimSubmittedEvent) -> None:
                handler(event)
                committed_event_ids.append(event.event_id)

            try:
                committed = 0
                deadline = time.monotonic() + timeout
                while committed < crash_after:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out before the simulated crash")
                    committed += int(
                        committed_consumer.process_next(
                            committed_handler,
                            timeout=0.5,
                        )
                    )
            finally:
                committed_consumer.close()

            # Reproduce the important crash window: the handler completes its
            # PostgreSQL/chain side effects, but the Kafka offset is not committed.
            raw_consumer = Consumer(settings.consumer_config())
            raw_consumer.subscribe([settings.topic])
            try:
                crash_message = _next_message(raw_consumer)
                crash_event = ClaimSubmittedEvent.from_json_bytes(
                    crash_message.value()
                )
                handler(crash_event)
                # No offset store or commit occurs before this consumer vanishes.
            finally:
                raw_consumer.close()

            # With three partitions, delivery order is deliberately not assumed
            # to match claim ID order. Use the events actually committed before
            # the crash when calculating completion and remaining backlog.
            unique_ids = set(committed_event_ids)
            unique_ids.add(crash_event.event_id)
            restarted_deliveries: list[str] = []
            points: list[RecoveryPoint] = []
            restarted = KafkaClaimEventConsumer(settings)
            restart_started = time.perf_counter_ns()

            def restarted_handler(event: ClaimSubmittedEvent) -> None:
                handler(event)
                restarted_deliveries.append(event.event_id)
                unique_ids.add(event.event_id)

            try:
                deadline = time.monotonic() + timeout
                while len(unique_ids) < backlog:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Recovered {len(unique_ids)} of {backlog} claims"
                        )
                    if restarted.process_next(restarted_handler, timeout=0.5):
                        elapsed_ms = (
                            time.perf_counter_ns() - restart_started
                        ) / 1_000_000
                        points.append(
                            RecoveryPoint(
                                run_id=run_id,
                                elapsed_after_restart_ms=elapsed_ms,
                                delivered_after_restart=len(restarted_deliveries),
                                unique_completed=len(unique_ids),
                                application_backlog=backlog - len(unique_ids),
                            )
                        )
            finally:
                restarted.close()

            recovery_ms = (time.perf_counter_ns() - restart_started) / 1_000_000
            completed = 0
            for event in events:
                record = repositories.assessments.get_by_event_id(event.event_id)
                completed += int(
                    record is not None and record.processing_status == "completed"
                )
            summary = RecoverySummary(
                run_id=run_id,
                backlog=backlog,
                committed_before_crash=crash_after,
                crash_event_id=crash_event.event_id,
                redelivered_crash_event=(
                    crash_event.event_id in restarted_deliveries
                ),
                delivered_after_restart=len(restarted_deliveries),
                unique_completed=completed,
                assessment_writes=registry.assessment_calls,
                duplicate_assessment_writes=max(
                    0,
                    registry.assessment_calls - backlog,
                ),
                lost_claims=max(0, backlog - completed),
                recovery_time_ms=recovery_ms,
            )
            if (
                completed != backlog
                or registry.assessment_calls != backlog
                or not summary.redelivered_crash_event
            ):
                raise RuntimeError(f"Recovery integrity check failed: {summary}")
            return points, summary
    finally:
        _delete_topic(admin, settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backlog", type=int, default=100)
    parser.add_argument("--crash-after", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=1_800)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("BENCHMARK_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument(
        "--kafka-bootstrap-servers",
        default=os.environ.get("BENCHMARK_KAFKA", DEFAULT_KAFKA),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/local/results"),
    )
    parser.add_argument("--keep-schema", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.backlog < 2 or not 0 < args.crash_after < args.backlog:
        raise SystemExit("crash-after must be between zero and backlog")
    run_id = datetime.now(UTC).strftime("recovery-%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    output_dir = args.output / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    parameters = {
        "backlog": args.backlog,
        "crash_after": args.crash_after,
        "consumers": 1,
        "kafka_partitions": 3,
    }
    write_json(
        output_dir / "manifest.json",
        repository_manifest(
            run_id=run_id,
            benchmark="recovery",
            parameters=parameters,
        ),
    )
    points, summary = run_recovery(
        run_id=run_id,
        backlog=args.backlog,
        crash_after=args.crash_after,
        timeout=args.timeout,
        database_url=args.database_url,
        kafka_bootstrap_servers=args.kafka_bootstrap_servers,
        keep_schema=args.keep_schema,
    )
    _write_csv(output_dir / "recovery-timeline.csv", points)
    write_json(output_dir / "recovery-summary.json", asdict(summary))
    print(
        f"Recovery: {summary.unique_completed}/{summary.backlog} complete, "
        f"replay={summary.redelivered_crash_event}, "
        f"duplicates={summary.duplicate_assessment_writes}, "
        f"recovery={summary.recovery_time_ms:.2f} ms"
    )
    print(f"Recovery benchmark evidence: {output_dir}")


if __name__ == "__main__":
    main()
