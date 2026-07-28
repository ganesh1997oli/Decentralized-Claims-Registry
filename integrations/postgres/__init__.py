"""PostgreSQL processing interfaces for claim features and assessments."""

from duplicates import DuplicateCheck, DuplicateMatch
from .assessment_repository import (
    AssessmentRecord,
    PostgresAssessmentRepository,
    PostgresConfigurationError,
    PostgresStorageError,
)
from .feature_processor import (
    FEATURE_VERSION,
    POLICY_FINGERPRINT_VERSION,
    ClaimFeatureConfigurationError,
    ClaimFeatureInput,
    ClaimFeatureProcessingError,
    ClaimFeatureProcessor,
    ClaimFeatureSnapshot,
)

__all__ = [
    "AssessmentRecord",
    "ClaimFeatureConfigurationError",
    "ClaimFeatureInput",
    "ClaimFeatureProcessingError",
    "ClaimFeatureProcessor",
    "ClaimFeatureSnapshot",
    "DuplicateCheck",
    "DuplicateMatch",
    "FEATURE_VERSION",
    "POLICY_FINGERPRINT_VERSION",
    "PostgresAssessmentRepository",
    "PostgresConfigurationError",
    "PostgresStorageError",
]
