"""Test normalization of Docker resource units."""

from __future__ import annotations

from benchmarks.local.collect_resources import parse_percentage, parse_quantity


def test_parse_quantity_supports_decimal_and_binary_units() -> None:
    assert parse_quantity("1.5MB") == 1_500_000
    assert parse_quantity("1.5 MiB") == 1_572_864
    assert parse_quantity("512B") == 512
    assert parse_quantity("unavailable") is None


def test_parse_percentage_preserves_numeric_value() -> None:
    assert parse_percentage("17.25%") == 17.25
    assert parse_percentage("unknown") is None
