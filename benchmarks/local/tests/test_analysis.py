"""Test statistical rules used by dissertation benchmark summaries."""

from __future__ import annotations

import pytest

from benchmarks.local.analyse_results import distribution, percentile


def test_percentile_interpolates_ordered_values() -> None:
    assert percentile([40.0, 10.0, 30.0, 20.0], 0.50) == pytest.approx(25.0)
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == pytest.approx(3.85)


def test_distribution_withholds_unstable_p99() -> None:
    small = distribution([float(value) for value in range(100)])
    retained = distribution([float(value) for value in range(1_000)])

    assert small["n"] == 100
    assert small["p50"] == pytest.approx(49.5)
    assert small["p99"] is None
    assert retained["p99"] == pytest.approx(989.01)


def test_empty_distribution_preserves_missingness() -> None:
    result = distribution([])

    assert result["n"] == 0
    assert result["mean"] is None
    assert result["p95"] is None
