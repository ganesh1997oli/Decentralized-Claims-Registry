"""Construct the focused PostgreSQL repositories from one connection module."""

from __future__ import annotations

from dataclasses import dataclass

from integrations.postgres.assessment_repository import (
    PostgresAssessmentRepository,
)
from integrations.postgres.database import PostgresDatabase
from integrations.postgres.duplicate_repository import PostgresDuplicateRepository
from integrations.postgres.feature_repository import PostgresFeatureRepository


@dataclass(frozen=True)
class PostgresRepositories:
    """The three persistence adapters used by the running application.

    Callers select the narrow repository they actually need. The bundle exists
    only to guarantee that all three share identical connection configuration.
    """

    assessments: PostgresAssessmentRepository
    duplicates: PostgresDuplicateRepository
    features: PostgresFeatureRepository
    database: PostgresDatabase

    @classmethod
    def from_database(cls, database: PostgresDatabase) -> PostgresRepositories:
        return cls(
            assessments=PostgresAssessmentRepository(database),
            duplicates=PostgresDuplicateRepository(database),
            features=PostgresFeatureRepository(database),
            database=database,
        )

    @classmethod
    def from_env(cls) -> PostgresRepositories:
        return cls.from_database(PostgresDatabase.from_env())
