"""Construct the focused PostgreSQL repositories from one connection module."""

from __future__ import annotations

from dataclasses import dataclass

from packages.integrations.postgres.assessment_repository import (
    PostgresAssessmentRepository,
)
from packages.integrations.postgres.claim_index_repository import (
    PostgresClaimIndexRepository,
)
from packages.integrations.postgres.database import PostgresDatabase
from packages.integrations.postgres.duplicate_repository import (
    PostgresDuplicateRepository,
)
from packages.integrations.postgres.feature_repository import PostgresFeatureRepository


@dataclass(frozen=True)
class PostgresRepositories:
    """The focused persistence adapters used by the running application.

    Callers select the narrow repository they actually need. The bundle exists
    only to guarantee that all adapters share identical connection configuration.
    """

    claims: PostgresClaimIndexRepository
    assessments: PostgresAssessmentRepository
    duplicates: PostgresDuplicateRepository
    features: PostgresFeatureRepository
    database: PostgresDatabase

    @classmethod
    def from_database(cls, database: PostgresDatabase) -> PostgresRepositories:
        return cls(
            claims=PostgresClaimIndexRepository(database),
            assessments=PostgresAssessmentRepository(database),
            duplicates=PostgresDuplicateRepository(database),
            features=PostgresFeatureRepository(database),
            database=database,
        )

    @classmethod
    def from_env(cls) -> PostgresRepositories:
        return cls.from_database(PostgresDatabase.from_env())
