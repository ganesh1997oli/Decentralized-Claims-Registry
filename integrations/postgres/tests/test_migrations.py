"""Tests for the explicit, checksummed PostgreSQL migration interface."""

from __future__ import annotations

from pathlib import Path

import pytest

from integrations.postgres import PostgresDatabase, PostgresStorageError
from integrations.postgres.migrations.runner import (
    Migration,
    PostgresMigrator,
    load_migrations,
)


class MigrationCursor:
    def __init__(
        self,
        *,
        applied_rows=(),
        version_table: str | None = "claims_schema_migrations",
    ) -> None:
        self.applied_rows = list(applied_rows)
        self.version_table = version_table
        self.executions = []
        self.last_statement = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, parameters=None):
        self.last_statement = str(statement)
        self.executions.append((statement, parameters))

    def fetchone(self):
        if "to_regclass" in self.last_statement:
            return {"table": self.version_table}
        return None

    def fetchall(self):
        if "FROM claims_schema_migrations" in self.last_statement:
            return self.applied_rows
        return []


class MigrationConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self._cursor


def database_for(cursor) -> PostgresDatabase:
    return PostgresDatabase(
        "postgresql://test",
        connect=lambda _url: MigrationConnection(cursor),
    )


def migration() -> Migration:
    return Migration(
        version="001",
        name="001_test.sql",
        checksum="known-checksum",
        sql="CREATE TABLE example (id INTEGER)",
    )


def test_checked_in_initial_migration_owns_all_application_tables():
    migrations = load_migrations()

    assert [item.version for item in migrations] == ["001"]
    assert "CREATE TABLE IF NOT EXISTS claim_assessments" in migrations[0].sql
    assert "CREATE TABLE IF NOT EXISTS claim_incident_fingerprints" in migrations[0].sql
    assert "CREATE TABLE IF NOT EXISTS claim_feature_snapshots" in migrations[0].sql


def test_upgrade_locks_applies_and_records_each_pending_migration():
    cursor = MigrationCursor(applied_rows=[])
    migrator = PostgresMigrator(database_for(cursor), migrations=(migration(),))

    applied = migrator.upgrade()

    assert applied == ("001",)
    assert "pg_advisory_xact_lock" in cursor.executions[0][0]
    assert any(item[0] == migration().sql for item in cursor.executions)
    insert = next(
        item
        for item in cursor.executions
        if "INSERT INTO claims_schema_migrations" in item[0]
    )
    assert insert[1] == ("001", "001_test.sql", "known-checksum")


def test_upgrade_is_a_no_op_when_the_checksum_matches():
    cursor = MigrationCursor(
        applied_rows=[
            {
                "version": "001",
                "name": "001_test.sql",
                "checksum": "known-checksum",
            }
        ]
    )
    migrator = PostgresMigrator(database_for(cursor), migrations=(migration(),))

    assert migrator.upgrade() == ()
    assert not any(item[0] == migration().sql for item in cursor.executions)


def test_migration_checksum_changes_are_rejected():
    cursor = MigrationCursor(
        applied_rows=[
            {
                "version": "001",
                "name": "001_test.sql",
                "checksum": "different-checksum",
            }
        ]
    )
    migrator = PostgresMigrator(database_for(cursor), migrations=(migration(),))

    with pytest.raises(PostgresStorageError, match="checksum changed"):
        migrator.require_current()


def test_migration_loader_rejects_unversioned_sql(tmp_path: Path):
    (tmp_path / "initial.sql").write_text("SELECT 1", encoding="utf-8")

    with pytest.raises(PostgresStorageError, match="Invalid migration filename"):
        load_migrations(tmp_path)
