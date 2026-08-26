"""Sample benchmark container and host resource use into a raw CSV file."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.local.adapters import repository_manifest, write_json

_QUANTITY = re.compile(r"^([0-9.]+)\s*([KMGT]?i?B)?$")
_MULTIPLIERS = {
    "B": 1,
    "KB": 1_000,
    "MB": 1_000**2,
    "GB": 1_000**3,
    "TB": 1_000**4,
    "KiB": 1_024,
    "MiB": 1_024**2,
    "GiB": 1_024**3,
    "TiB": 1_024**4,
}


def parse_quantity(value: str) -> int | None:
    """Convert the human byte units emitted by Docker into integer bytes."""

    match = _QUANTITY.fullmatch(value.strip())
    if not match:
        return None
    number, unit = match.groups()
    return round(float(number) * _MULTIPLIERS.get(unit or "B", 1))


def parse_percentage(value: str) -> float | None:
    try:
        return float(value.strip().removesuffix("%"))
    except ValueError:
        return None


def docker_rows(prefix: str) -> list[dict[str, object]]:
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in result.stdout.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(raw.get("Name", ""))
        if prefix and not name.startswith(prefix):
            continue
        memory_parts = str(raw.get("MemUsage", "")).split("/")
        rows.append(
            {
                "source_type": "container",
                "source": name,
                "cpu_percent": parse_percentage(str(raw.get("CPUPerc", ""))),
                "memory_percent": parse_percentage(str(raw.get("MemPerc", ""))),
                "memory_bytes": parse_quantity(memory_parts[0]),
                "memory_limit_bytes": (
                    parse_quantity(memory_parts[1]) if len(memory_parts) > 1 else None
                ),
                "rss_kib": None,
                "process_count": raw.get("PIDs"),
            }
        )
    return rows


def process_row(pid: int) -> dict[str, object] | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "%cpu=,%mem=,rss=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        values = result.stdout.split()
        if len(values) != 3:
            return None
        cpu, memory, rss = values
        return {
            "source_type": "process",
            "source": str(pid),
            "cpu_percent": float(cpu),
            "memory_percent": float(memory),
            "memory_bytes": int(rss) * 1_024,
            "memory_limit_bytes": None,
            "rss_kib": int(rss),
            "process_count": 1,
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--container-prefix", default="claims-benchmark-")
    parser.add_argument("--pid", type=int, action="append", default=[])
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.interval <= 0 or args.duration < 0:
        raise SystemExit("interval must be positive and duration cannot be negative")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        args.output.with_suffix(".manifest.json"),
        repository_manifest(
            run_id=datetime.now(UTC).strftime("resources-%Y%m%dT%H%M%SZ"),
            benchmark="resources",
            parameters={
                "interval_seconds": args.interval,
                "duration_seconds": args.duration,
                "container_prefix": args.container_prefix,
                "process_ids": args.pid,
            },
        ),
    )
    fields = [
        "timestamp_utc",
        "elapsed_seconds",
        "host_load_1m",
        "source_type",
        "source",
        "cpu_percent",
        "memory_percent",
        "memory_bytes",
        "memory_limit_bytes",
        "rss_kib",
        "process_count",
    ]
    started = time.monotonic()
    with args.output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        try:
            while not args.duration or time.monotonic() - started < args.duration:
                timestamp = datetime.now(UTC).isoformat()
                elapsed = time.monotonic() - started
                try:
                    load_1m = os.getloadavg()[0]
                except OSError:
                    load_1m = None
                rows = docker_rows(args.container_prefix)
                rows.extend(
                    row
                    for pid in args.pid
                    if (row := process_row(pid)) is not None
                )
                if not rows:
                    rows = [
                        {
                            "source_type": "host",
                            "source": platform_name(),
                            "cpu_percent": None,
                            "memory_percent": None,
                            "memory_bytes": None,
                            "memory_limit_bytes": None,
                            "rss_kib": None,
                            "process_count": None,
                        }
                    ]
                for row in rows:
                    writer.writerow(
                        {
                            "timestamp_utc": timestamp,
                            "elapsed_seconds": round(elapsed, 6),
                            "host_load_1m": load_1m,
                            **row,
                        }
                    )
                stream.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass


def platform_name() -> str:
    return os.uname().sysname if hasattr(os, "uname") else os.name


if __name__ == "__main__":
    main()
