"""Benchmark the real Kafka/PostgreSQL/XGBoost pipeline on one host."""

from __future__ import annotations

import argparse
import csv
import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from confluent_kafka.admin import AdminClient, NewTopic
from web3 import Web3

from apps.backend.app.models import ClaimSubmission
from apps.backend.app.submission_auth import ClaimAuthorizationSigner, InsurerPrincipal
from benchmarks.local.adapters import (
    BENCHMARK_AUTHORIZATION_KEY,
    BENCHMARK_CONTRACT,
    BENCHMARK_FINGERPRINT_KEY,
    BenchmarkAssessmentRegistry,
    BenchmarkPayloadStore,
    DisposableRepositories,
    TimingScorer,
    repository_manifest,
    write_json,
)
from packages.duplicates import CrossInsurerDuplicateDetector
from packages.integrations.kafka import (
    ClaimSubmittedEvent,
    KafkaClaimEventConsumer,
    KafkaClaimEventPublisher,
    KafkaSettings,
)
from packages.integrations.kafka.scoring_worker import ClaimScoringHandler
from packages.integrations.postgres import ClaimFeatureProcessor
from packages.model.xgboost_scorer import XGBoostFraudScorer

DEFAULT_DATABASE_URL = (
    "postgresql://claims:claims-benchmark@127.0.0.1:55432/claims_benchmark"
)
DEFAULT_KAFKA = "127.0.0.1:19092"
SIGNER = "0x1111111111111111111111111111111111111111"
EVENT_TIME = int(datetime(2026, 8, 20, tzinfo=UTC).timestamp())


@dataclass(frozen=True)
class PipelineObservation:
    run_id: str
    scenario_id: str
    repetition: int
    offered_rate: float
    event_id: str
    claim_id: int
    publish_ack_ms: float
    queue_delay_ms: float
    inference_ms: float | None
    processing_ms: float
    end_to_end_ms: float
    success: bool
    error: str


@dataclass(frozen=True)
class PipelineScenario:
    run_id: str
    scenario_id: str
    repetition: int
    offered_rate: float
    attempted: int
    successful: int
    errors: int
    elapsed_seconds: float
    throughput_events_per_second: float
    assessment_writes: int


class PipelineRecorder:
    """Wrap the production handler and retain one timing row per event."""

    def __init__(
        self,
        *,
        run_id: str,
        scenario_id: str,
        repetition: int,
        offered_rate: float,
        handler: ClaimScoringHandler,
        scorer: TimingScorer,
    ) -> None:
        self.run_id = run_id
        self.scenario_id = scenario_id
        self.repetition = repetition
        self.offered_rate = offered_rate
        self.handler = handler
        self.scorer = scorer
        self.enqueued_ns: dict[str, int] = {}
        self.publish_ms: dict[str, float] = {}
        self.observations: list[PipelineObservation] = []
        self.errors: list[Exception] = []
        self._lock = threading.Lock()
        self.changed = threading.Condition(self._lock)

    def mark_enqueued(self, event_id: str, started_ns: int) -> None:
        with self._lock:
            self.enqueued_ns[event_id] = started_ns

    def mark_published(self, event_id: str, duration_ms: float) -> None:
        with self._lock:
            self.publish_ms[event_id] = duration_ms

    def __call__(self, event: ClaimSubmittedEvent) -> None:
        handler_started = time.perf_counter_ns()
        self.scorer.bind(event.event_id)
        error = ""
        try:
            self.handler(event)
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
            raise
        finally:
            handler_ended = time.perf_counter_ns()
            with self.changed:
                enqueued = self.enqueued_ns.get(event.event_id, handler_started)
                self.observations.append(
                    PipelineObservation(
                        run_id=self.run_id,
                        scenario_id=self.scenario_id,
                        repetition=self.repetition,
                        offered_rate=self.offered_rate,
                        event_id=event.event_id,
                        claim_id=event.claim_id,
                        publish_ack_ms=self.publish_ms.get(event.event_id, 0.0),
                        queue_delay_ms=(handler_started - enqueued) / 1_000_000,
                        inference_ms=self.scorer.duration_ms(event.event_id),
                        processing_ms=(handler_ended - handler_started) / 1_000_000,
                        end_to_end_ms=(handler_ended - enqueued) / 1_000_000,
                        success=not error,
                        error=error,
                    )
                )
                self.changed.notify_all()

    def wait_for_count(self, count: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self.changed:
            while len(self.observations) < count and not self.errors:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Processed {len(self.observations)} of {count} events"
                    )
                self.changed.wait(timeout=min(remaining, 1.0))
            if self.errors:
                raise RuntimeError("Pipeline consumer failed") from self.errors[0]


def parse_rates(value: str) -> tuple[float, ...]:
    rates = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    if not rates or any(rate < 0 for rate in rates):
        raise argparse.ArgumentTypeError("offered rates must be zero or positive")
    return rates


def _settings(bootstrap_servers: str, scenario_id: str) -> KafkaSettings:
    identity = scenario_id.replace("_", "-").lower()
    return KafkaSettings.from_mapping(
        {
            "KAFKA_ENABLED": "true",
            "KAFKA_BOOTSTRAP_SERVERS": bootstrap_servers,
            "KAFKA_CLAIM_SUBMITTED_TOPIC": f"claims.benchmark.{identity}",
            "KAFKA_CLIENT_ID": f"claims-benchmark-{identity}",
            "KAFKA_CONSUMER_GROUP_ID": f"claims-benchmark-{identity}",
            "KAFKA_DELIVERY_TIMEOUT_MS": "30000",
            "KAFKA_CONSUMER_POLL_SECONDS": "0.25",
        }
    )


def _create_topic(settings: KafkaSettings) -> AdminClient:
    admin = AdminClient({"bootstrap.servers": settings.bootstrap_servers})
    future = admin.create_topics(
        [NewTopic(settings.topic, num_partitions=3, replication_factor=1)]
    )[settings.topic]
    future.result(timeout=30)
    return admin


def _delete_topic(admin: AdminClient, settings: KafkaSettings) -> None:
    future = admin.delete_topics([settings.topic], operation_timeout=15)[settings.topic]
    future.result(timeout=30)


def claim_payload(
    claim_id: int,
    authorization: ClaimAuthorizationSigner,
) -> bytes:
    claim = ClaimSubmission.model_validate(
        {
            "insurerId": "northstar-mutual",
            "claimReference": f"pipeline-claim-{claim_id}",
            "policyReference": f"pipeline-policy-{claim_id}",
            "claimType": ("theft" if claim_id % 4 == 0 else "collision"),
            "incidentDate": "2026-07-13",
            "claimAmountUsd": 2_500 + (claim_id % 1_000),
            "policyPremiumUsd": 480 + (claim_id % 40),
            "vehicleAge": 1 + (claim_id % 20),
            "vehicleType": ("suv" if claim_id % 3 == 0 else "sedan"),
            "country": ("Ghana" if claim_id % 5 == 0 else "Nigeria"),
            "regionType": ("rural" if claim_id % 7 == 0 else "urban"),
            "thirdPartyInjuryFlag": claim_id % 11 == 0,
            "totalLossFlag": claim_id % 13 == 0,
            "description": "Synthetic Kafka performance-baseline claim",
            "evidence": [],
        }
    )
    principal = InsurerPrincipal(
        insurer_id="northstar-mutual",
        credential_id="pipeline-benchmark-v1",
        signer_address=SIGNER,
        permitted_operations=frozenset({"submit_claim"}),
        daily_quota=1_000_000,
        rate_limit_exempt=True,
    )
    return authorization.authorized_claim_bytes(claim, principal)


def submitted_event(claim_id: int, payload: bytes) -> ClaimSubmittedEvent:
    transaction_hash = f"0x{claim_id:064x}"
    return ClaimSubmittedEvent.create(
        chain_id=31_337,
        contract_address=BENCHMARK_CONTRACT,
        claim_id=claim_id,
        claimant=SIGNER,
        claim_hash=Web3.keccak(payload).hex(),
        data_pointer=f"ipfs://claim{claim_id}",
        block_number=1_000 + claim_id,
        block_hash=f"0x{(10_000 + claim_id):064x}",
        transaction_hash=transaction_hash,
        log_index=0,
        event_timestamp=EVENT_TIME + claim_id,
    )


def _publish_phase(
    *,
    publisher: KafkaClaimEventPublisher,
    payload_store: BenchmarkPayloadStore,
    authorization: ClaimAuthorizationSigner,
    recorder: PipelineRecorder,
    first_claim_id: int,
    count: int,
    rate: float,
) -> list[ClaimSubmittedEvent]:
    events: list[ClaimSubmittedEvent] = []
    next_release = time.perf_counter()
    for claim_id in range(first_claim_id, first_claim_id + count):
        if rate > 0:
            remaining = next_release - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            next_release += 1 / rate
        payload = claim_payload(claim_id, authorization)
        event = submitted_event(claim_id, payload)
        payload_store.put_pointer(event.data_pointer, payload)
        started_ns = time.perf_counter_ns()
        recorder.mark_enqueued(event.event_id, started_ns)
        publisher.publish(event)
        recorder.mark_published(
            event.event_id,
            (time.perf_counter_ns() - started_ns) / 1_000_000,
        )
        events.append(event)
    return events


def run_scenario(
    *,
    run_id: str,
    repetition: int,
    offered_rate: float,
    events: int,
    warmup_events: int,
    timeout: float,
    database_url: str,
    kafka_bootstrap_servers: str,
    keep_schema: bool,
) -> tuple[list[PipelineObservation], PipelineScenario]:
    rate_label = str(offered_rate).replace(".", "p")
    scenario_id = f"{run_id}-r{repetition}-rate{rate_label}-{uuid4().hex[:6]}"
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
            timing_scorer = TimingScorer(XGBoostFraudScorer.from_env())
            handler = ClaimScoringHandler(
                ipfs=payload_store,
                scorer=timing_scorer,
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
            recorder = PipelineRecorder(
                run_id=run_id,
                scenario_id=scenario_id,
                repetition=repetition,
                offered_rate=offered_rate,
                handler=handler,
                scorer=timing_scorer,
            )
            consumer = KafkaClaimEventConsumer(settings)
            publisher = KafkaClaimEventPublisher(settings)
            target_count = warmup_events + events

            def consume() -> None:
                try:
                    while len(recorder.observations) < target_count:
                        consumer.process_next(recorder, timeout=0.25)
                except Exception as exc:  # noqa: BLE001 - cross-thread propagation
                    with recorder.changed:
                        recorder.errors.append(exc)
                        recorder.changed.notify_all()

            thread = threading.Thread(target=consume, name=scenario_id, daemon=True)
            thread.start()
            try:
                if warmup_events:
                    _publish_phase(
                        publisher=publisher,
                        payload_store=payload_store,
                        authorization=authorization,
                        recorder=recorder,
                        first_claim_id=1,
                        count=warmup_events,
                        rate=0,
                    )
                    recorder.wait_for_count(warmup_events, timeout)

                measurement_started = time.perf_counter()
                measured_events = _publish_phase(
                    publisher=publisher,
                    payload_store=payload_store,
                    authorization=authorization,
                    recorder=recorder,
                    first_claim_id=warmup_events + 1,
                    count=events,
                    rate=offered_rate,
                )
                recorder.wait_for_count(target_count, timeout)
                elapsed = time.perf_counter() - measurement_started
            finally:
                publisher.close()
                consumer.close()
                thread.join(timeout=5)

            measured_ids = {event.event_id for event in measured_events}
            measured = [
                replace(
                    item,
                    # A fast consumer can complete while the synchronous
                    # publisher is still returning its delivery receipt. Patch
                    # that acknowledged duration into the retained immutable
                    # observation after both sides have completed.
                    publish_ack_ms=recorder.publish_ms.get(
                        item.event_id,
                        item.publish_ack_ms,
                    ),
                )
                for item in recorder.observations
                if item.event_id in measured_ids
            ]
            completed = 0
            for event in measured_events:
                record = repositories.assessments.get_by_event_id(event.event_id)
                completed += int(
                    record is not None and record.processing_status == "completed"
                )
            successful = sum(item.success for item in measured)
            if completed != events or successful != events:
                raise RuntimeError(
                    f"Integrity check failed: completed={completed}, "
                    f"successful={successful}, expected={events}"
                )
            scenario = PipelineScenario(
                run_id=run_id,
                scenario_id=scenario_id,
                repetition=repetition,
                offered_rate=offered_rate,
                attempted=events,
                successful=successful,
                errors=events - successful,
                elapsed_seconds=elapsed,
                throughput_events_per_second=(successful / elapsed if elapsed else 0),
                assessment_writes=registry.assessment_calls - warmup_events,
            )
            return measured, scenario
    finally:
        _delete_topic(admin, settings)


def _write_csv(path: Path, rows: list[object]) -> None:
    if not rows:
        return
    values = [asdict(row) for row in rows]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(values[0]))
        writer.writeheader()
        writer.writerows(values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rates", type=parse_rates, default=(0.0, 5.0, 10.0))
    parser.add_argument("--events-per-rate", type=int, default=100)
    parser.add_argument("--warmup-events", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900)
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
    parser.add_argument("--keep-schemas", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if (
        args.events_per_rate < 1
        or args.warmup_events < 0
        or args.repetitions < 1
        or args.timeout <= 0
    ):
        raise SystemExit("event counts, repetitions, and timeout must be positive")
    run_id = datetime.now(UTC).strftime("pipeline-%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    output_dir = args.output / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    observations: list[PipelineObservation] = []
    scenarios: list[PipelineScenario] = []
    parameters = {
        "rates": list(args.rates),
        "events_per_rate": args.events_per_rate,
        "warmup_events": args.warmup_events,
        "repetitions": args.repetitions,
        "kafka_partitions": 3,
        "consumers": 1,
    }
    write_json(
        output_dir / "manifest.json",
        repository_manifest(
            run_id=run_id,
            benchmark="pipeline",
            parameters=parameters,
        ),
    )

    try:
        for repetition in range(1, args.repetitions + 1):
            for rate in args.rates:
                measured, scenario = run_scenario(
                    run_id=run_id,
                    repetition=repetition,
                    offered_rate=rate,
                    events=args.events_per_rate,
                    warmup_events=args.warmup_events,
                    timeout=args.timeout,
                    database_url=args.database_url,
                    kafka_bootstrap_servers=args.kafka_bootstrap_servers,
                    keep_schema=args.keep_schemas,
                )
                observations.extend(measured)
                scenarios.append(scenario)
                print(
                    f"Pipeline r{repetition} rate={rate:g}: "
                    f"{scenario.successful}/{scenario.attempted} successful, "
                    f"{scenario.throughput_events_per_second:.2f} events/s"
                )
    finally:
        _write_csv(output_dir / "raw-pipeline-timings.csv", observations)
        _write_csv(output_dir / "pipeline-scenarios.csv", scenarios)

    print(f"Pipeline benchmark evidence: {output_dir}")


if __name__ == "__main__":
    main()
