"""Summarise retained benchmark CSV files and generate dissertation graphs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/claims-benchmark-matplotlib")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def percentile(values: list[float], quantile: float) -> float | None:
    """Calculate an interpolated percentile without hiding a small sample."""

    if not values:
        return None
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "mean": statistics.fmean(values) if values else None,
        "standard_deviation": statistics.stdev(values) if len(values) > 1 else None,
        "minimum": min(values) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        # A p99 from fewer than 1,000 values is too unstable for this report.
        "p99": percentile(values, 0.99) if len(values) >= 1_000 else None,
        "maximum": max(values) if values else None,
    }


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _float(row: dict[str, str], key: str) -> float | None:
    value = row.get(key, "").strip()
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _true(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes"}


def _metric(rows: list[dict[str, str]], name: str) -> list[float]:
    return [
        value
        for row in rows
        if (value := _float(row, name)) is not None
    ]


def analyse_http(input_dir: Path) -> dict[str, Any] | None:
    timing_files = list(input_dir.rglob("raw-http-timings.csv"))
    if not timing_files:
        return None
    rows = [row for path in timing_files for row in _rows(path)]
    scenarios = [
        row
        for path in input_dir.rglob("http-scenarios.csv")
        for row in _rows(path)
    ]
    groups: dict[int, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[int(row["concurrency"])].append(row)
    throughput: dict[int, list[float]] = defaultdict(list)
    for row in scenarios:
        value = _float(row, "throughput_claims_per_second")
        if value is not None:
            throughput[int(row["concurrency"])].append(value)

    summary: dict[str, Any] = {}
    for concurrency, group in sorted(groups.items()):
        successful = [row for row in group if _true(row["success"])]
        summary[str(concurrency)] = {
            "attempted": len(group),
            "successful": len(successful),
            "errors": len(group) - len(successful),
            "error_rate": (len(group) - len(successful)) / len(group),
            "prepare_ms": distribution(_metric(successful, "prepare_ms")),
            "authorize_ms": distribution(_metric(successful, "authorize_ms")),
            "end_to_end_ms": distribution(_metric(successful, "end_to_end_ms")),
            "throughput_claims_per_second": distribution(throughput[concurrency]),
        }
    return summary


def analyse_pipeline(input_dir: Path) -> dict[str, Any] | None:
    timing_files = list(input_dir.rglob("raw-pipeline-timings.csv"))
    if not timing_files:
        return None
    rows = [row for path in timing_files for row in _rows(path)]
    scenarios = [
        row
        for path in input_dir.rglob("pipeline-scenarios.csv")
        for row in _rows(path)
    ]
    groups: dict[float, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[float(row["offered_rate"])].append(row)
    throughput: dict[float, list[float]] = defaultdict(list)
    for row in scenarios:
        value = _float(row, "throughput_events_per_second")
        if value is not None:
            throughput[float(row["offered_rate"])].append(value)

    summary: dict[str, Any] = {}
    for rate, group in sorted(groups.items()):
        successful = [row for row in group if _true(row["success"])]
        summary[f"{rate:g}"] = {
            "attempted": len(group),
            "successful": len(successful),
            "errors": len(group) - len(successful),
            "error_rate": (len(group) - len(successful)) / len(group),
            "publish_ack_ms": distribution(_metric(successful, "publish_ack_ms")),
            "queue_delay_ms": distribution(_metric(successful, "queue_delay_ms")),
            "inference_ms": distribution(_metric(successful, "inference_ms")),
            "processing_ms": distribution(_metric(successful, "processing_ms")),
            "end_to_end_ms": distribution(_metric(successful, "end_to_end_ms")),
            "throughput_events_per_second": distribution(throughput[rate]),
        }
    return summary


def analyse_recovery(input_dir: Path) -> list[dict[str, Any]]:
    summaries = []
    for path in input_dir.rglob("recovery-summary.json"):
        summaries.append(json.loads(path.read_text(encoding="utf-8")))
    return summaries


def analyse_gas(input_dir: Path) -> dict[str, Any] | None:
    files = list(input_dir.rglob("gas-results.json"))
    if not files:
        return None
    groups: dict[str, list[float]] = defaultdict(list)
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for observation in raw.get("observations", []):
            groups[str(observation["operation"])].append(
                float(observation["gas_used"])
            )
    return {
        operation: distribution(values)
        for operation, values in sorted(groups.items())
    }


def analyse_resources(input_dir: Path) -> dict[str, Any] | None:
    files = list(input_dir.rglob("resource-usage.csv"))
    if not files:
        return None
    rows = [row for path in files for row in _rows(path)]
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["source"]].append(row)
    return {
        "samples": len(rows),
        "host_load_1m": distribution(_metric(rows, "host_load_1m")),
        "sources": {
            source: {
                "samples": len(group),
                "cpu_percent": distribution(_metric(group, "cpu_percent")),
                "memory_bytes": distribution(_metric(group, "memory_bytes")),
                "memory_percent": distribution(
                    _metric(group, "memory_percent")
                ),
                "process_count": distribution(_metric(group, "process_count")),
            }
            for source, group in sorted(groups.items())
        },
    }


def _line_graph(
    *,
    x: list[float],
    series: list[tuple[str, list[float | None]]],
    xlabel: str,
    ylabel: str,
    title: str,
    output: Path,
) -> None:
    figure, axis = plt.subplots(figsize=(7.2, 4.4))
    for label, values in series:
        axis.plot(x, values, marker="o", linewidth=2, label=label)
    axis.set_xlabel(xlabel)
    axis.set_ylabel(ylabel)
    axis.set_title(title)
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output, dpi=180)
    plt.close(figure)


def graph_http(summary: dict[str, Any], output_dir: Path) -> None:
    levels = sorted(int(value) for value in summary)
    _line_graph(
        x=[float(value) for value in levels],
        series=[
            (
                "p50",
                [summary[str(value)]["end_to_end_ms"]["p50"] for value in levels],
            ),
            (
                "p95",
                [summary[str(value)]["end_to_end_ms"]["p95"] for value in levels],
            ),
        ],
        xlabel="Concurrent clients",
        ylabel="Prepare + sign + authorize latency (ms)",
        title="HTTP claim-intake latency on one host",
        output=output_dir / "latency-vs-concurrency.png",
    )
    _line_graph(
        x=[float(value) for value in levels],
        series=[
            (
                "Mean across repetitions",
                [
                    summary[str(value)]["throughput_claims_per_second"]["mean"]
                    for value in levels
                ],
            )
        ],
        xlabel="Concurrent clients",
        ylabel="Successful claims per second",
        title="HTTP claim-intake throughput on one host",
        output=output_dir / "throughput-vs-concurrency.png",
    )


def graph_pipeline(summary: dict[str, Any], output_dir: Path) -> None:
    rates = sorted(float(value) for value in summary)
    keys = [f"{value:g}" for value in rates]
    _line_graph(
        x=rates,
        series=[
            (
                "Queue p95",
                [summary[key]["queue_delay_ms"]["p95"] for key in keys],
            ),
            (
                "Processing p95",
                [summary[key]["processing_ms"]["p95"] for key in keys],
            ),
            (
                "Inference p95",
                [summary[key]["inference_ms"]["p95"] for key in keys],
            ),
        ],
        xlabel="Offered events per second (0 = burst)",
        ylabel="Latency (ms)",
        title="Kafka scoring-stage latency on one host",
        output=output_dir / "pipeline-stage-latency.png",
    )


def graph_recovery(input_dir: Path, output_dir: Path) -> None:
    files = list(input_dir.rglob("recovery-timeline.csv"))
    if not files:
        return
    rows = _rows(files[0])
    x = [float(row["elapsed_after_restart_ms"]) / 1_000 for row in rows]
    y = [float(row["application_backlog"]) for row in rows]
    _line_graph(
        x=x,
        series=[("Uncompleted unique claims", y)],
        xlabel="Seconds after consumer restart",
        ylabel="Application backlog",
        title="Replay recovery after an uncommitted side effect",
        output=output_dir / "kafka-lag-recovery.png",
    )


def graph_gas(input_dir: Path, output_dir: Path) -> None:
    values: dict[str, list[float]] = defaultdict(list)
    for path in input_dir.rglob("gas-results.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for observation in raw.get("observations", []):
            values[str(observation["operation"])].append(
                float(observation["gas_used"])
            )
    if not values:
        return
    figure, axis = plt.subplots(figsize=(7.2, 4.4))
    labels = sorted(values)
    axis.boxplot([values[label] for label in labels], tick_labels=labels)
    axis.set_ylabel("Gas used")
    axis.set_title("Hardhat gas distribution by operation")
    axis.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_dir / "gas-distribution.png", dpi=180)
    plt.close(figure)


def graph_resources(input_dir: Path, output_dir: Path) -> None:
    files = list(input_dir.rglob("resource-usage.csv"))
    if not files:
        return
    rows = [row for path in files for row in _rows(path)]
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["source"]].append(row)

    figure, (cpu_axis, memory_axis) = plt.subplots(
        2,
        1,
        figsize=(7.2, 6.8),
        sharex=True,
    )
    for source, group in sorted(grouped.items()):
        elapsed = [_float(row, "elapsed_seconds") for row in group]
        cpu = [_float(row, "cpu_percent") for row in group]
        memory_mib = [
            (value / (1_024**2) if value is not None else None)
            for row in group
            if (value := _float(row, "memory_bytes")) is not None
        ]
        memory_x = [
            elapsed[index]
            for index, row in enumerate(group)
            if _float(row, "memory_bytes") is not None
        ]
        cpu_axis.plot(elapsed, cpu, linewidth=1.7, label=source)
        memory_axis.plot(memory_x, memory_mib, linewidth=1.7, label=source)
    cpu_axis.set_ylabel("CPU (%)")
    cpu_axis.grid(alpha=0.25)
    cpu_axis.legend()
    memory_axis.set_xlabel("Elapsed seconds")
    memory_axis.set_ylabel("Memory (MiB)")
    memory_axis.grid(alpha=0.25)
    memory_axis.legend()
    figure.suptitle("Benchmark dependency resource use")
    figure.tight_layout()
    figure.savefig(output_dir / "resource-usage.png", dpi=180)
    plt.close(figure)


def write_checksums(input_dir: Path, output_dir: Path) -> None:
    lines = []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.name == "checksums.sha256":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(input_dir)}")
    (output_dir / "checksums.sha256").write_text(
        os.linesep.join(lines) + os.linesep,
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.input.exists():
        raise SystemExit(f"Input path does not exist: {args.input}")
    output_dir = args.output or args.input / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "http": analyse_http(args.input),
        "pipeline": analyse_pipeline(args.input),
        "recovery": analyse_recovery(args.input),
        "gas": analyse_gas(args.input),
        "resources": analyse_resources(args.input),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + os.linesep,
        encoding="utf-8",
    )
    if summary["http"]:
        graph_http(summary["http"], output_dir)
    if summary["pipeline"]:
        graph_pipeline(summary["pipeline"], output_dir)
    graph_recovery(args.input, output_dir)
    graph_gas(args.input, output_dir)
    graph_resources(args.input, output_dir)
    write_checksums(args.input, output_dir)
    print(f"Analysis written to {output_dir}")


if __name__ == "__main__":
    main()
