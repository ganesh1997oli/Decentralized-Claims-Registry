"""Drive the real gasless FastAPI routes against deterministic dependencies."""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiohttp
from eth_account import Account
from eth_account.messages import encode_typed_data

from benchmarks.local.adapters import (
    benchmark_account,
    drop_benchmark_schema,
    normalize_hex,
    repository_manifest,
    schema_for_run,
    write_json,
)

DEFAULT_DATABASE_URL = (
    "postgresql://claims:claims-benchmark@127.0.0.1:55432/claims_benchmark"
)


@dataclass(frozen=True)
class HttpObservation:
    run_id: str
    scenario_id: str
    repetition: int
    concurrency: int
    request_number: int
    prepare_ms: float | None
    sign_ms: float | None
    authorize_ms: float | None
    end_to_end_ms: float
    prepare_status: int | None
    authorize_status: int | None
    success: bool
    error: str


@dataclass(frozen=True)
class HttpScenario:
    run_id: str
    scenario_id: str
    repetition: int
    concurrency: int
    attempted: int
    successful: int
    errors: int
    elapsed_seconds: float
    throughput_claims_per_second: float


def parse_levels(value: str) -> tuple[int, ...]:
    levels = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not levels or any(level < 1 for level in levels):
        raise argparse.ArgumentTypeError("concurrency levels must be positive integers")
    return levels


def _claim(run_id: str, request_number: int) -> dict[str, object]:
    suffix = f"{run_id[-12:]}-{request_number}"
    return {
        "insurerId": "northstar-mutual",
        "claimReference": f"bench-claim-{suffix}",
        "policyReference": f"bench-policy-{suffix}",
        "claimType": "collision",
        "incidentDate": "2026-07-13",
        "claimAmountUsd": 2_500 + (request_number % 500),
        "policyPremiumUsd": 480,
        "vehicleAge": 6,
        "vehicleType": "sedan",
        "country": "Nigeria",
        "regionType": "urban",
        "thirdPartyInjuryFlag": False,
        "totalLossFlag": False,
        "description": "Synthetic local performance-baseline claim",
        "evidence": [],
    }


def _token(run_id: str, request_number: int) -> str:
    return f"benchmark-{run_id}-{request_number}"


def _signature(token: str, typed_data: dict[str, object]) -> str:
    account = benchmark_account(token)
    signable = encode_typed_data(full_message=typed_data)
    signed = Account.sign_message(signable, private_key=account.key)
    return normalize_hex(signed.signature.hex())


async def _one_claim(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    run_id: str,
    scenario_id: str,
    repetition: int,
    concurrency: int,
    request_number: int,
) -> HttpObservation:
    token = _token(run_id, request_number)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": f"benchmark-{run_id}-{request_number}",
        "Content-Type": "application/json",
    }
    total_started = time.perf_counter_ns()
    prepare_ms: float | None = None
    sign_ms: float | None = None
    authorize_ms: float | None = None
    prepare_status: int | None = None
    authorize_status: int | None = None
    error = ""
    try:
        prepare_started = time.perf_counter_ns()
        async with session.post(
            f"{base_url}/claims/gasless/prepare",
            headers=headers,
            json=_claim(run_id, request_number),
        ) as response:
            prepare_status = response.status
            prepared = await response.json()
        prepare_ms = (time.perf_counter_ns() - prepare_started) / 1_000_000
        if prepare_status != 201:
            error = f"prepare_http_{prepare_status}:{prepared.get('detail', '')}"
            raise RuntimeError(error)

        sign_started = time.perf_counter_ns()
        signature = _signature(token, prepared["typed_data"])
        sign_ms = (time.perf_counter_ns() - sign_started) / 1_000_000

        authorize_started = time.perf_counter_ns()
        async with session.post(
            (
                f"{base_url}/claims/gasless/"
                f"{prepared['submission_id']}/authorize"
            ),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"signature": signature},
        ) as response:
            authorize_status = response.status
            authorized = await response.json()
        authorize_ms = (time.perf_counter_ns() - authorize_started) / 1_000_000
        if authorize_status != 202:
            error = f"authorize_http_{authorize_status}:{authorized.get('detail', '')}"
        elif authorized.get("state") != "authorized":
            error = f"unexpected_state:{authorized.get('state')}"
    except Exception as exc:  # noqa: BLE001 - every failed attempt is evidence
        if not error:
            error = f"{type(exc).__name__}:{exc}"

    end_to_end_ms = (time.perf_counter_ns() - total_started) / 1_000_000
    return HttpObservation(
        run_id=run_id,
        scenario_id=scenario_id,
        repetition=repetition,
        concurrency=concurrency,
        request_number=request_number,
        prepare_ms=prepare_ms,
        sign_ms=sign_ms,
        authorize_ms=authorize_ms,
        end_to_end_ms=end_to_end_ms,
        prepare_status=prepare_status,
        authorize_status=authorize_status,
        success=not error,
        error=error,
    )


async def _load(
    *,
    base_url: str,
    run_id: str,
    scenario_id: str,
    repetition: int,
    concurrency: int,
    requests: int,
    request_offset: int,
) -> tuple[list[HttpObservation], float]:
    queue: asyncio.Queue[int] = asyncio.Queue()
    for number in range(request_offset, request_offset + requests):
        queue.put_nowait(number)
    observations: list[HttpObservation] = []
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(limit=concurrency)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:

        async def worker() -> None:
            while True:
                try:
                    number = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                observation = await _one_claim(
                    session,
                    base_url=base_url,
                    run_id=run_id,
                    scenario_id=scenario_id,
                    repetition=repetition,
                    concurrency=concurrency,
                    request_number=number,
                )
                observations.append(observation)
                queue.task_done()

        started = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed = time.perf_counter() - started
    observations.sort(key=lambda item: item.request_number)
    return observations, elapsed


def _wait_for_server(url: str, process: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Benchmark API exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError("Benchmark API did not become ready")


def _start_server(
    *,
    database_url: str,
    schema_name: str,
    port: int,
    log_path: Path,
) -> tuple[subprocess.Popen, object]:
    environment = os.environ.copy()
    environment.update(
        {
            "BENCHMARK_MODE": "enabled",
            "BENCHMARK_DATABASE_URL": database_url,
            "BENCHMARK_SCHEMA": schema_name,
            "MAX_CLAIM_BODY_BYTES": "16384",
        }
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "benchmarks.local.api_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=environment,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return process, log_file


def _stop_server(process: subprocess.Popen, log_file: object) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    log_file.close()


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
    parser.add_argument("--concurrency", type=parse_levels, default=(1, 5, 10, 20))
    parser.add_argument("--requests-per-level", type=int, default=100)
    parser.add_argument("--warmup-requests", type=int, default=10)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--port", type=int, default=18_000)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("BENCHMARK_DATABASE_URL", DEFAULT_DATABASE_URL),
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
    if args.requests_per_level < 1 or args.warmup_requests < 0 or args.repetitions < 1:
        raise SystemExit("request counts and repetitions must be positive")
    run_id = datetime.now(UTC).strftime("http-%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    output_dir = args.output / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    all_observations: list[HttpObservation] = []
    scenarios: list[HttpScenario] = []
    request_offset = 0

    parameters = {
        "concurrency": list(args.concurrency),
        "requests_per_level": args.requests_per_level,
        "warmup_requests": args.warmup_requests,
        "repetitions": args.repetitions,
        "api_workers": 1,
    }
    write_json(
        output_dir / "manifest.json",
        repository_manifest(
            run_id=run_id,
            benchmark="http",
            parameters=parameters,
        ),
    )

    try:
        for repetition in range(1, args.repetitions + 1):
            for concurrency in args.concurrency:
                scenario_id = f"{run_id}-r{repetition}-c{concurrency}"
                schema_name = schema_for_run(scenario_id)
                process, log_file = _start_server(
                    database_url=args.database_url,
                    schema_name=schema_name,
                    port=args.port,
                    log_path=output_dir / f"api-{scenario_id}.log",
                )
                try:
                    _wait_for_server(
                        f"http://127.0.0.1:{args.port}/health/live",
                        process,
                    )
                    if args.warmup_requests:
                        asyncio.run(
                            _load(
                                base_url=f"http://127.0.0.1:{args.port}",
                                run_id=run_id,
                                scenario_id=f"{scenario_id}-warmup",
                                repetition=repetition,
                                concurrency=concurrency,
                                requests=args.warmup_requests,
                                request_offset=request_offset,
                            )
                        )
                        request_offset += args.warmup_requests
                    observations, elapsed = asyncio.run(
                        _load(
                            base_url=f"http://127.0.0.1:{args.port}",
                            run_id=run_id,
                            scenario_id=scenario_id,
                            repetition=repetition,
                            concurrency=concurrency,
                            requests=args.requests_per_level,
                            request_offset=request_offset,
                        )
                    )
                    request_offset += args.requests_per_level
                    successful = sum(item.success for item in observations)
                    all_observations.extend(observations)
                    scenarios.append(
                        HttpScenario(
                            run_id=run_id,
                            scenario_id=scenario_id,
                            repetition=repetition,
                            concurrency=concurrency,
                            attempted=len(observations),
                            successful=successful,
                            errors=len(observations) - successful,
                            elapsed_seconds=elapsed,
                            throughput_claims_per_second=(
                                successful / elapsed if elapsed else 0.0
                            ),
                        )
                    )
                    print(
                        f"HTTP r{repetition} c{concurrency}: "
                        f"{successful}/{len(observations)} successful, "
                        f"{successful / elapsed:.2f} claims/s"
                    )
                finally:
                    _stop_server(process, log_file)
                    if not args.keep_schemas:
                        drop_benchmark_schema(args.database_url, schema_name)
    finally:
        _write_csv(output_dir / "raw-http-timings.csv", all_observations)
        _write_csv(output_dir / "http-scenarios.csv", scenarios)

    print(f"HTTP benchmark evidence: {output_dir}")


if __name__ == "__main__":
    main()
