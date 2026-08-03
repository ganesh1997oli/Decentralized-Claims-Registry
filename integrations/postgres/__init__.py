"""PostgreSQL processing interfaces for claim features and assessments."""

from duplicates import DuplicateCheck, DuplicateMatch

from .assessment_repository import (
    PostgresAssessmentRepository,
)
from .database import (
    PostgresConfigurationError,
    PostgresDatabase,
    PostgresStorageError,
)
from .duplicate_repository import PostgresDuplicateRepository
from .feature_processor import (
    FEATURE_VERSION,
    POLICY_FINGERPRINT_VERSION,
    ClaimFeatureConfigurationError,
    ClaimFeatureInput,
    ClaimFeatureProcessingError,
    ClaimFeatureProcessor,
    ClaimFeatureSnapshot,
)
from .feature_repository import PostgresFeatureRepository
from .migrations import MigrationStatus, PostgresMigrator
from .records import AssessmentRecord
from .repositories import PostgresRepositories

__all__ = [
    "FEATURE_VERSION",
    "POLICY_FINGERPRINT_VERSION",
    "AssessmentRecord",
    "ClaimFeatureConfigurationError",
    "ClaimFeatureInput",
    "ClaimFeatureProcessingError",
    "ClaimFeatureProcessor",
    "ClaimFeatureSnapshot",
    "DuplicateCheck",
    "DuplicateMatch",
    "MigrationStatus",
    "PostgresAssessmentRepository",
    "PostgresConfigurationError",
    "PostgresDatabase",
    "PostgresDuplicateRepository",
    "PostgresFeatureRepository",
    "PostgresMigrator",
    "PostgresRepositories",
    "PostgresStorageError",
]
