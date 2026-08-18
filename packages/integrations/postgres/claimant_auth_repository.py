"""Durable one-time wallet challenges for public claimant authentication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from packages.integrations.postgres.database import PostgresDatabase
from packages.integrations.postgres.records import ClaimantAuthChallengeRecord


class ClaimantAuthChallengeError(RuntimeError):
    """Raised when a challenge cannot be issued or consumed safely."""


class ClaimantAuthChallengeRateLimitError(ClaimantAuthChallengeError):
    """Raised before challenge creation when a durable abuse limit is reached."""

    def __init__(self, message: str, *, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)


class PostgresClaimantAuthChallengeRepository:
    """Persist and atomically consume claimant wallet challenges.

    The repository owns concurrency and rate-count decisions because those
    guarantees must hold across every API replica. The authentication module
    owns message construction, signature recovery, and session-token policy.
    """

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    @staticmethod
    def _record(row) -> ClaimantAuthChallengeRecord | None:
        if row is None:
            return None
        return ClaimantAuthChallengeRecord(
            challenge_id=UUID(str(row["challenge_id"])),
            wallet_address=str(row["wallet_address"]),
            nonce=str(row["nonce"]),
            message=str(row["message"]),
            client_fingerprint=str(row["client_fingerprint"]),
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            consumed_at=row.get("consumed_at"),
        )

    def issue(
        self,
        record: ClaimantAuthChallengeRecord,
        *,
        client_limit_per_minute: int,
        wallet_limit_per_minute: int,
    ) -> ClaimantAuthChallengeRecord:
        """Insert one challenge after serialized client and wallet rate checks."""

        if record.issued_at.tzinfo is None or record.expires_at.tzinfo is None:
            raise ValueError("Challenge timestamps must be timezone-aware")
        issued_at = record.issued_at.astimezone(UTC)
        expires_at = record.expires_at.astimezone(UTC)
        minute_start = issued_at - timedelta(minutes=1)
        normalized_wallet = record.wallet_address.lower()

        with self.database.cursor() as cursor:
            # Separate advisory locks prevent two replicas from observing the
            # same pre-insert counts for either abuse-control dimension.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"claimant-auth-client:{record.client_fingerprint}",),
            )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"claimant-auth-wallet:{normalized_wallet}",),
            )
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE client_fingerprint = %s
                    ) AS client_count,
                    COUNT(*) FILTER (
                        WHERE lower(wallet_address) = %s
                    ) AS wallet_count
                FROM claimant_auth_challenges
                WHERE issued_at >= %s
                  AND (
                    client_fingerprint = %s
                    OR lower(wallet_address) = %s
                  )
                """,
                (
                    record.client_fingerprint,
                    normalized_wallet,
                    minute_start,
                    record.client_fingerprint,
                    normalized_wallet,
                ),
            )
            counts = cursor.fetchone() or {}
            if int(counts.get("client_count", 0)) >= client_limit_per_minute:
                raise ClaimantAuthChallengeRateLimitError(
                    "This client has requested too many wallet challenges"
                )
            if int(counts.get("wallet_count", 0)) >= wallet_limit_per_minute:
                raise ClaimantAuthChallengeRateLimitError(
                    "This wallet has requested too many authentication challenges"
                )

            # Expired rows are audit evidence for a short period, then removed
            # opportunistically to keep this abuse-control table bounded.
            cursor.execute(
                """
                DELETE FROM claimant_auth_challenges
                WHERE expires_at < %s - INTERVAL '1 day'
                """,
                (issued_at,),
            )
            cursor.execute(
                """
                INSERT INTO claimant_auth_challenges (
                    challenge_id, wallet_address, nonce, message,
                    client_fingerprint, issued_at, expires_at
                ) VALUES (%s, lower(%s), %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    record.challenge_id,
                    record.wallet_address,
                    record.nonce,
                    record.message,
                    record.client_fingerprint,
                    issued_at,
                    expires_at,
                ),
            )
            saved = self._record(cursor.fetchone())
            assert saved is not None
            return saved

    def get(self, challenge_id: UUID) -> ClaimantAuthChallengeRecord | None:
        """Return a challenge for signature verification without mutating it."""

        with self.database.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM claimant_auth_challenges WHERE challenge_id = %s",
                (challenge_id,),
            )
            return self._record(cursor.fetchone())

    def consume(
        self,
        challenge_id: UUID,
        *,
        wallet_address: str,
        now: datetime,
    ) -> ClaimantAuthChallengeRecord:
        """Atomically consume one unexpired challenge for its recovered wallet."""

        if now.tzinfo is None:
            raise ValueError("Challenge consumption time must be timezone-aware")
        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE claimant_auth_challenges
                SET consumed_at = %s
                WHERE challenge_id = %s
                  AND lower(wallet_address) = lower(%s)
                  AND consumed_at IS NULL
                  AND expires_at >= %s
                RETURNING *
                """,
                (now, challenge_id, wallet_address, now),
            )
            consumed = self._record(cursor.fetchone())
            if consumed is None:
                raise ClaimantAuthChallengeError(
                    "Authentication challenge is invalid, expired, or already used"
                )
            return consumed
