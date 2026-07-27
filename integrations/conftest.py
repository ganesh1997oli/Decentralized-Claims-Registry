"""Disposable infrastructure fixtures shared by integration tests."""

import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from integrations.postgres import PostgresAssessmentRepository


@pytest.fixture
def postgres_repository():
    """Provide a repository isolated in a uniquely named PostgreSQL schema."""

    database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")

    schema_name = f"claims_test_{uuid4().hex}"
    with psycopg.connect(database_url) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )

    def connect(url: str):
        return psycopg.connect(
            url,
            options=f"-c search_path={schema_name}",
            row_factory=dict_row,
        )

    repository = PostgresAssessmentRepository(database_url, connect=connect)
    try:
        repository.ensure_schema()
        yield repository
    finally:
        # The generated and quoted identifier constrains CASCADE to this test's
        # disposable schema; no application schema or data can be selected.
        with psycopg.connect(database_url) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
