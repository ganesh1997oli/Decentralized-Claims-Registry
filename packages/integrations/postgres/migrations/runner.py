"""Apply ordered SQL migrations exactly once with checksum verification.

The runner is intentionally small and uses the project's existing psycopg
dependency. Every upgrade executes inside one database transaction and holds a
PostgreSQL advisory transaction lock, preventing two deployment processes from
applying schema changes concurrently.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from packages.integrations.postgres.database import (
    PostgresDatabase,
    PostgresStorageError,
)
from packages.observability import configure_logging, get_event_logger

logger = get_event_logger(__name__)
VERSIONS_DIRECTORY = Path(__file__).with_name("versions")
MIGRATION_NAME = re.compile(r"(?P<version>\d{3,})_[a-z0-9_]+\.sql\Z")
MIGRATION_LOCK = "decentralized-claims-registry:schema-migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    checksum: str
    sql: str


@dataclass(frozen=True)
class MigrationStatus:
    current: bool
    applied: tuple[str, ...]
    pending: tuple[str, ...]


def load_migrations(directory: Path = VERSIONS_DIRECTORY) -> tuple[Migration, ...]:
    """Load deterministic migration files and reject ambiguous filenames."""

    migrations: list[Migration] = []
    seen_versions: set[str] = set()
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise PostgresStorageError(f"Invalid migration filename: {path.name}")
        version = match.group("version")
        if version in seen_versions:
            raise PostgresStorageError(f"Duplicate migration version: {version}")
        seen_versions.add(version)
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                version=version,
                name=path.name,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                sql=sql,
            )
        )
    if not migrations:
        raise PostgresStorageError("No PostgreSQL migrations were found")
    return tuple(migrations)


class PostgresMigrator:
    """Upgrade and verify one PostgreSQL schema through a small interface."""

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        migrations: tuple[Migration, ...] | None = None,
    ) -> None:
        self.database = database
        self.migrations = migrations or load_migrations()

    @staticmethod
    def _ensure_version_table(cursor) -> None:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS claims_schema_migrations (
                version TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    def upgrade(self) -> tuple[str, ...]:
        """Apply all pending migrations and return the newly applied versions."""

        applied_now: list[str] = []
        with self.database.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (MIGRATION_LOCK,),
            )
            self._ensure_version_table(cursor)
            cursor.execute(
                "SELECT version, name, checksum FROM claims_schema_migrations"
            )
            applied = {str(row["version"]): row for row in cursor.fetchall()}
            known_versions = {migration.version for migration in self.migrations}
            unknown = sorted(set(applied) - known_versions)
            if unknown:
                raise PostgresStorageError(
                    "Database contains unknown migration version(s): "
                    + ", ".join(unknown)
                )

            for migration in self.migrations:
                existing = applied.get(migration.version)
                if existing is not None:
                    if existing["checksum"] != migration.checksum:
                        raise PostgresStorageError(
                            f"Applied migration {migration.version} checksum changed"
                        )
                    continue
                cursor.execute(migration.sql)
                cursor.execute(
                    """
                    INSERT INTO claims_schema_migrations (version, name, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.name, migration.checksum),
                )
                applied_now.append(migration.version)
                logger.info(
                    "database.migration_applied",
                    version=migration.version,
                    migration=migration.name,
                )
        return tuple(applied_now)

    def status(self) -> MigrationStatus:
        """Return applied/pending versions without modifying the schema."""

        with self.database.cursor() as cursor:
            cursor.execute("SELECT to_regclass('claims_schema_migrations') AS table")
            row = cursor.fetchone()
            if row is None or row["table"] is None:
                return MigrationStatus(
                    current=False,
                    applied=(),
                    pending=tuple(m.version for m in self.migrations),
                )
            cursor.execute(
                "SELECT version, name, checksum FROM claims_schema_migrations"
            )
            applied_rows = {str(item["version"]): item for item in cursor.fetchall()}

        known_versions = {migration.version for migration in self.migrations}
        unknown = sorted(set(applied_rows) - known_versions)
        if unknown:
            raise PostgresStorageError(
                "Database contains unknown migration version(s): " + ", ".join(unknown)
            )
        for migration in self.migrations:
            existing = applied_rows.get(migration.version)
            if existing is not None and existing["checksum"] != migration.checksum:
                raise PostgresStorageError(
                    f"Applied migration {migration.version} checksum changed"
                )

        applied = tuple(
            migration.version
            for migration in self.migrations
            if migration.version in applied_rows
        )
        pending = tuple(
            migration.version
            for migration in self.migrations
            if migration.version not in applied_rows
        )
        return MigrationStatus(current=not pending, applied=applied, pending=pending)

    def require_current(self) -> None:
        """Fail readiness when deploy-time migrations have not completed."""

        status = self.status()
        if not status.current:
            pending = ", ".join(status.pending) or "migration metadata"
            raise PostgresStorageError(
                f"PostgreSQL schema is not current; pending: {pending}"
            )


def main() -> None:
    configure_logging("claims-database-migrator")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("upgrade", "check"),
        default="upgrade",
    )
    args = parser.parse_args()
    migrator = PostgresMigrator(PostgresDatabase.from_env())
    if args.command == "upgrade":
        applied = migrator.upgrade()
        logger.info(
            "database.migrations_current",
            applied_versions=list(applied),
        )
        return
    migrator.require_current()
    logger.info("database.migrations_current", applied_versions=[])
