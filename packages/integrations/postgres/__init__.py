"""PostgreSQL processing interfaces for claim features and assessments."""

from packages.duplicates import DuplicateCheck, DuplicateMatch

from .assessment_repository import (
    PostgresAssessmentRepository,
)
from .assessor_outcome_repository import PostgresAssessorOutcomeRepository
from .claim_index_repository import (
    PostgresClaimIndexCheckpoint,
    PostgresClaimIndexRepository,
    claim_index_event_id,
)
from .claimant_auth_repository import (
    ClaimantAuthChallengeError,
    ClaimantAuthChallengeRateLimitError,
    PostgresClaimantAuthChallengeRepository,
)
from .coverage_decision_repository import (
    CoverageDecisionConflictError,
    PostgresCoverageDecisionRepository,
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
from .gasless_submission_repository import (
    GaslessSubmissionConflictError,
    GaslessSubmissionError,
    GaslessSubmissionLimitError,
    GaslessSubmissionNotFoundError,
    PostgresGaslessSubmissionRepository,
    SignedRelayTransaction,
)
from .migrations import MigrationStatus, PostgresMigrator
from .records import (
    AssessmentRecord,
    AssessorOutcomeRecord,
    ClaimantAuthChallengeRecord,
    ClaimIndexEventPage,
    ClaimIndexEventRecord,
    ClaimIndexOperationsSnapshot,
    ClaimIndexReconciliationRecord,
    ClaimIndexStatus,
    CoverageDecisionProposalRecord,
    CoverageDecisionStatus,
    GaslessSubmissionRecord,
    GaslessSubmissionState,
    HumanFraudOutcome,
    IndexedClaim,
)
from .repositories import PostgresRepositories

__all__ = [
    "FEATURE_VERSION",
    "POLICY_FINGERPRINT_VERSION",
    "AssessmentRecord",
    "AssessorOutcomeRecord",
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
    "ClaimantAuthChallengeError",
    "ClaimantAuthChallengeRateLimitError",
    "ClaimantAuthChallengeRecord",
    "CoverageDecisionConflictError",
    "CoverageDecisionProposalRecord",
    "CoverageDecisionStatus",
    "DuplicateCheck",
    "DuplicateMatch",
    "GaslessSubmissionConflictError",
    "GaslessSubmissionError",
    "GaslessSubmissionLimitError",
    "GaslessSubmissionNotFoundError",
    "GaslessSubmissionRecord",
    "GaslessSubmissionState",
    "HumanFraudOutcome",
    "IndexedClaim",
    "MigrationStatus",
    "PostgresAssessmentRepository",
    "PostgresAssessorOutcomeRepository",
    "PostgresClaimIndexCheckpoint",
    "PostgresClaimIndexRepository",
    "PostgresClaimantAuthChallengeRepository",
    "PostgresConfigurationError",
    "PostgresCoverageDecisionRepository",
    "PostgresDatabase",
    "PostgresDuplicateRepository",
    "PostgresFeatureRepository",
    "PostgresGaslessSubmissionRepository",
    "PostgresMigrator",
    "PostgresRepositories",
    "PostgresStorageError",
    "SignedRelayTransaction",
    "claim_index_event_id",
]
