"""Stable scoring result types shared by model and persistence adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FraudReason:
    """One human-readable feature contribution to a fraud score."""

    feature: str
    label: str
    contribution: float


@dataclass(frozen=True)
class FraudScore:
    """Model-independent fraud result consumed by the scoring workflow."""

    probability: float
    score_basis_points: int
    threshold: float
    flagged: bool
    model_version: str
    reasons: tuple[FraudReason, ...]
