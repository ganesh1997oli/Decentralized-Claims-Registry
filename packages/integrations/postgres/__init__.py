"""PostgreSQL processing interfaces for claim features and assessments."""

from packages.duplicates import DuplicateCheck, DuplicateMatch

from .assessment_repository import (
    PostgresAssessmentRepository,
)
from .claim_index_repository import (
    PostgresClaimIndexCheckpoint,
    PostgresClaimIndexRepository,
    claim_index_event_id,
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
from .records import (
    AssessmentRecord,
    ClaimIndexEventPage,
    ClaimIndexEventRecord,
    ClaimIndexOperationsSnapshot,
    ClaimIndexReconciliationRecord,
    ClaimIndexStatus,
    IndexedClaim,
)
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
    "ClaimIndexEventPage",
    "ClaimIndexEventRecord",
    "ClaimIndexOperationsSnapshot",
    "ClaimIndexReconciliationRecord",
    "ClaimIndexStatus",
    "DuplicateCheck",
    "DuplicateMatch",
    "IndexedClaim",
    "MigrationStatus",
    "PostgresAssessmentRepository",
    "PostgresClaimIndexCheckpoint",
    "PostgresClaimIndexRepository",
    "PostgresConfigurationError",
    "PostgresDatabase",
    "PostgresDuplicateRepository",
    "PostgresFeatureRepository",
    "PostgresMigrator",
    "PostgresRepositories",
    "PostgresStorageError",
    "claim_index_event_id",
]
