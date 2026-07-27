"""Persistence interfaces for claim assessments and duplicate fingerprints."""

from duplicates import DuplicateCheck, DuplicateMatch
from .assessment_repository import (
    AssessmentRecord,
    PostgresAssessmentRepository,
    PostgresConfigurationError,
    PostgresStorageError,
)

__all__ = [
    "AssessmentRecord",
    "DuplicateCheck",
    "DuplicateMatch",
    "PostgresAssessmentRepository",
    "PostgresConfigurationError",
    "PostgresStorageError",
]
