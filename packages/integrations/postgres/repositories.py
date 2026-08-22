"""Construct the focused PostgreSQL repositories from one connection module."""

from __future__ import annotations

from dataclasses import dataclass

from packages.integrations.postgres.assessment_repository import (
    PostgresAssessmentRepository,
)
from packages.integrations.postgres.assessor_outcome_repository import (
    PostgresAssessorOutcomeRepository,
)
from packages.integrations.postgres.claim_index_repository import (
    PostgresClaimIndexRepository,
)
from packages.integrations.postgres.claimant_auth_repository import (
    PostgresClaimantAuthChallengeRepository,
)
from packages.integrations.postgres.database import PostgresDatabase
from packages.integrations.postgres.duplicate_repository import (
    PostgresDuplicateRepository,
)
from packages.integrations.postgres.feature_repository import PostgresFeatureRepository
from packages.integrations.postgres.gasless_submission_repository import (
    PostgresGaslessSubmissionRepository,
)


@dataclass(frozen=True)
class PostgresRepositories:
    """The focused persistence adapters used by the running application.

    Callers select only the repository they need. The bundle exists
    only to guarantee that all adapters share identical connection configuration.
    """

    claims: PostgresClaimIndexRepository
    assessments: PostgresAssessmentRepository
    assessor_outcomes: PostgresAssessorOutcomeRepository
    duplicates: PostgresDuplicateRepository
    features: PostgresFeatureRepository
    gasless_submissions: PostgresGaslessSubmissionRepository
    claimant_auth_challenges: PostgresClaimantAuthChallengeRepository
    database: PostgresDatabase

    @classmethod
    def from_database(cls, database: PostgresDatabase) -> PostgresRepositories:
        """Construct every focused repository over identical connection settings.

        Repositories still open independent transactions per operation; sharing the
        small database adapter guarantees consistent URL, row factory, and failure
        translation without creating a global live connection.
        """

        return cls(
            claims=PostgresClaimIndexRepository(database),
            assessments=PostgresAssessmentRepository(database),
            assessor_outcomes=PostgresAssessorOutcomeRepository(database),
            duplicates=PostgresDuplicateRepository(database),
            features=PostgresFeatureRepository(database),
            gasless_submissions=PostgresGaslessSubmissionRepository(database),
            claimant_auth_challenges=PostgresClaimantAuthChallengeRepository(
                database
            ),
            database=database,
        )

    @classmethod
    def from_env(cls) -> PostgresRepositories:
        """Build the repository bundle from the required ``DATABASE_URL``."""

        return cls.from_database(PostgresDatabase.from_env())
