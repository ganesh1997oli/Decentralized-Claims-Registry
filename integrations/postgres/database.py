"""Shared PostgreSQL connection handling for claim persistence modules.

Repositories receive this module instead of constructing driver connections
themselves.  The small interface keeps configuration, transaction lifetime and
safe error translation consistent without exposing psycopg to business logic.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class PostgresConfigurationError(ValueError):
    """Raised when PostgreSQL cannot be configured safely."""


class PostgresStorageError(RuntimeError):
    """Raised when PostgreSQL cannot complete an application operation."""


def default_connect(database_url: str) -> Any:
    """Open a dictionary-row psycopg connection without importing it at startup."""

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise PostgresConfigurationError(
            "Install integrations/postgres/requirements.txt to use PostgreSQL"
        ) from exc
    return psycopg.connect(database_url, row_factory=dict_row)


class PostgresDatabase:
    """Own connection configuration and one-transaction cursor lifetimes.

    A repository operation gets exactly one connection and cursor. Psycopg's
    context manager commits on success and rolls back on failure, so callers do
    not need to remember transaction cleanup. Tests inject a local adapter at
    this seam; production uses :func:`default_connect`.
    """

    def __init__(
        self,
        database_url: str,
        *,
        connect: Callable[[str], Any] = default_connect,
    ) -> None:
        if not database_url.strip():
            raise PostgresConfigurationError("DATABASE_URL cannot be empty")
        self.database_url = database_url
        self._connect = connect

    @classmethod
    def from_env(cls) -> PostgresDatabase:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise PostgresConfigurationError(
                "DATABASE_URL is required for PostgreSQL claim storage"
            )
        return cls(database_url)

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        """Yield one transactional cursor and hide driver-specific failures."""

        try:
            with (
                self._connect(self.database_url) as connection,
                connection.cursor() as cursor,
            ):
                yield cursor
        except (PostgresConfigurationError, PostgresStorageError):
            raise
        except Exception as exc:
            # Do not leak database URLs, SQL text, credentials or driver details
            # through public FastAPI responses. The exception chain remains
            # available to trusted logs and debuggers.
            raise PostgresStorageError(
                "PostgreSQL claim storage is unavailable"
            ) from exc

    def ping(self) -> None:
        """Run the smallest useful readiness query."""

        with self.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
            if row is None:
                raise PostgresStorageError("PostgreSQL readiness query returned no row")
