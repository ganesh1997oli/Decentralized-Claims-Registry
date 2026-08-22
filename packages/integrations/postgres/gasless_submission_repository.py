"""Persistent idempotency, sponsorship limits, and relay outbox state.

The table managed here is both a user-visible workflow record and a
transactional outbox. Its state machine is monotonic::

    preparing -> prepared -> authorized -> signed -> broadcast -> confirmed
         |           |            |          |           |
         +-----------+------------+----------+-----------+--> failed/expired

The API owns transitions through ``authorized``; the isolated relayer owns the
remaining transitions. SQL predicates such as ``WHERE state = 'prepared'`` act
as compare-and-set guards, so two replicas cannot silently move the same row in
different directions.

Most importantly, signed raw Ethereum bytes are committed in ``signed`` before
they are sent to an RPC node. If the process crashes after broadcasting, a new
worker can safely rebroadcast the identical bytes and hash rather than signing
a different transaction for the already-reserved relayer nonce.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .database import PostgresDatabase, PostgresStorageError
from .records import GaslessSubmissionRecord, gasless_submission_from_row

ACTIVE_STATES = ("preparing", "prepared", "authorized", "signed", "broadcast")
# An API replica that dies during IPFS/RPC preparation must not reserve a
# submitter's forwarder nonce forever. Only the unsigned ``preparing`` lease is
# reclaimed; signed or broadcast records need receipt reconciliation instead.
PREPARATION_LEASE = timedelta(minutes=10)


class GaslessSubmissionError(PostgresStorageError):
    """Base failure for the persisted sponsored-submission workflow."""


class GaslessSubmissionNotFoundError(GaslessSubmissionError):
    """Raised when a principal cannot access a requested submission."""


class GaslessSubmissionConflictError(GaslessSubmissionError):
    """Raised when a submission cannot make the requested state transition."""


class GaslessSubmissionLimitError(GaslessSubmissionError):
    """Raised when stored sponsorship capacity has been exhausted."""

    def __init__(self, message: str, *, retry_after: int) -> None:
        """Store a positive retry delay suitable for an HTTP ``Retry-After``."""

        super().__init__(message)
        self.retry_after = max(1, retry_after)


@dataclass(frozen=True)
class SignedRelayTransaction:
    """A deterministic EOA transaction persisted before it is broadcast.

    ``raw_transaction`` is the signed byte sequence Ethereum hashes and
    executes. Persisting it together with its derived hash makes replay after a
    crash deterministic and provides evidence that a nonce was not reused for
    different calldata.
    """

    nonce: int
    raw_transaction: str
    transaction_hash: str
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


class PostgresGaslessSubmissionRepository:
    """Own every stored transition, lock, and invariant in the relay lifecycle.

    Keeping the state machine in SQL transactions makes behavior consistent
    across multiple API or relayer replicas. Callers request transitions; they
    do not read a row, make an unlocked decision, and write it back later.
    Advisory locks protect cross-row invariants such as per-signer active work
    and monotonic allocation of the relayer account's Ethereum nonce.
    """

    def __init__(self, database: PostgresDatabase) -> None:
        """Bind state-machine operations to the configured PostgreSQL database."""

        self.database = database

    @staticmethod
    def _record(row) -> GaslessSubmissionRecord:
        """Convert a returned row or raise the workflow's stable not-found error."""

        record = gasless_submission_from_row(row)
        if record is None:
            raise GaslessSubmissionNotFoundError("Gasless submission was not found")
        return record

    def begin_preparation(
        self,
        *,
        submission_id: UUID,
        credential_id: str,
        insurer_id: str,
        signer_address: str,
        chain_id: int,
        contract_address: str,
        forwarder_address: str,
        idempotency_key_hash: str,
        request_fingerprint: str,
        client_fingerprint: str,
        insurer_minute_limit: int,
        client_minute_limit: int,
        daily_quota: int,
        bypass_limits: bool,
        now: datetime,
        submission_kind: str = "insurer",
        claimant_address: str | None = None,
        insurer_address: str | None = None,
        claimant_commitment: str | None = None,
        policy_id: str | None = None,
        permit_issuer_address: str | None = None,
    ) -> tuple[GaslessSubmissionRecord, bool]:
        """Create one preparation or return the matching idempotent record.

        PostgreSQL advisory locks serialize decisions for one credential and one
        forwarder signer across every API replica. Rate counts and the insert
        therefore observe one consistent transaction rather than per-process
        state. The boolean is true only for the caller responsible for external
        preparation work; a retry receives the original record and false.
        """

        if now.tzinfo is None:
            raise ValueError("Gasless preparation time must be timezone-aware")
        now = now.astimezone(UTC)
        minute_start = now - timedelta(minutes=1)
        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
        signer_scope = (
            f"{chain_id}:{forwarder_address.lower()}:{signer_address.lower()}"
        )

        with self.database.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"gasless-credential:{credential_id}",),
            )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"gasless-signer:{signer_scope}",),
            )
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'failed', updated_at = %s,
                    last_error_code = 'preparation_lease_expired'
                WHERE chain_id = %s
                  AND lower(forwarder_address) = lower(%s)
                  AND lower(signer_address) = lower(%s)
                  AND state = 'preparing'
                  AND updated_at < %s
                """,
                (
                    now,
                    chain_id,
                    forwarder_address,
                    signer_address,
                    now - PREPARATION_LEASE,
                ),
            )
            # Expired unsigned requests no longer reserve the submitter's forwarder
            # nonce. Signed/broadcast transactions remain active until reconciled.
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'expired', updated_at = %s,
                    last_error_code = 'signature_deadline_expired'
                WHERE chain_id = %s
                  AND lower(forwarder_address) = lower(%s)
                  AND lower(signer_address) = lower(%s)
                  AND state IN ('prepared', 'authorized')
                  AND deadline < %s
                """,
                (
                    now,
                    chain_id,
                    forwarder_address,
                    signer_address,
                    int(now.timestamp()),
                ),
            )
            cursor.execute(
                """
                SELECT * FROM gasless_claim_submissions
                WHERE credential_id = %s AND idempotency_key_hash = %s
                FOR UPDATE
                """,
                (credential_id, idempotency_key_hash),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise GaslessSubmissionConflictError(
                        "Idempotency-Key was already used for a different claim"
                    )
                return self._record(existing), False

            cursor.execute(
                """
                SELECT submission_id FROM gasless_claim_submissions
                WHERE chain_id = %s
                  AND lower(forwarder_address) = lower(%s)
                  AND lower(signer_address) = lower(%s)
                  AND state = ANY(%s)
                LIMIT 1
                FOR UPDATE
                """,
                (chain_id, forwarder_address, signer_address, list(ACTIVE_STATES)),
            )
            if cursor.fetchone() is not None:
                raise GaslessSubmissionConflictError(
                    "This wallet already has an active gasless submission"
                )

            if not bypass_limits:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE credential_id = %s) AS insurer_minute,
                        COUNT(*) FILTER (WHERE client_fingerprint = %s) AS client_minute
                    FROM gasless_claim_submissions
                    WHERE created_at >= %s
                      AND (credential_id = %s OR client_fingerprint = %s)
                    """,
                    (
                        credential_id,
                        client_fingerprint,
                        minute_start,
                        credential_id,
                        client_fingerprint,
                    ),
                )
                counts = cursor.fetchone() or {}
                if int(counts.get("insurer_minute", 0)) >= insurer_minute_limit:
                    raise GaslessSubmissionLimitError(
                        "This claimant has reached the sponsored per-minute limit",
                        retry_after=60,
                    )
                if int(counts.get("client_minute", 0)) >= client_minute_limit:
                    raise GaslessSubmissionLimitError(
                        "This client has reached its sponsored per-minute limit",
                        retry_after=60,
                    )
                cursor.execute(
                    """
                    SELECT COUNT(*) AS daily_usage
                    FROM gasless_claim_submissions
                    WHERE credential_id = %s AND created_at >= %s
                    """,
                    (credential_id, day_start),
                )
                daily = cursor.fetchone() or {}
                if int(daily.get("daily_usage", 0)) >= daily_quota:
                    tomorrow = day_start + timedelta(days=1)
                    raise GaslessSubmissionLimitError(
                        "This claimant has reached the sponsored daily quota",
                        retry_after=int((tomorrow - now).total_seconds()),
                    )

            cursor.execute(
                """
                INSERT INTO gasless_claim_submissions (
                    submission_id, credential_id, insurer_id, signer_address,
                    chain_id, contract_address, forwarder_address,
                    idempotency_key_hash, request_fingerprint,
                    client_fingerprint, state, submission_kind,
                    claimant_address, insurer_address, claimant_commitment,
                    policy_id, permit_issuer_address,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, lower(%s), lower(%s), %s, %s, %s,
                    'preparing', %s, lower(%s), lower(%s), %s, %s, lower(%s),
                    %s, %s
                )
                RETURNING *
                """,
                (
                    submission_id,
                    credential_id,
                    insurer_id,
                    signer_address,
                    chain_id,
                    contract_address,
                    forwarder_address,
                    idempotency_key_hash,
                    request_fingerprint,
                    client_fingerprint,
                    submission_kind,
                    claimant_address,
                    insurer_address,
                    claimant_commitment,
                    policy_id,
                    permit_issuer_address,
                    now,
                    now,
                ),
            )
            return self._record(cursor.fetchone()), True

    def mark_prepared(
        self,
        submission_id: UUID,
        *,
        claim_hash: str,
        data_pointer: str,
        call_data: str,
        forwarder_nonce: int,
        forward_gas: int,
        deadline: int,
    ) -> GaslessSubmissionRecord:
        """Commit the complete submitter-signable request and release its lease.

        Updating only ``preparing`` makes this a compare-and-set transition.
        Missing or concurrently changed rows fail instead of overwriting a later
        authorization, relay attempt, or terminal state.
        """

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'prepared', claim_hash = %s, data_pointer = %s,
                    call_data = %s, forwarder_nonce = %s, forward_gas = %s,
                    deadline = %s, updated_at = NOW(), last_error_code = NULL
                WHERE submission_id = %s AND state = 'preparing'
                RETURNING *
                """,
                (
                    claim_hash,
                    data_pointer,
                    call_data,
                    forwarder_nonce,
                    forward_gas,
                    deadline,
                    submission_id,
                ),
            )
            return self._record(cursor.fetchone())

    def mark_preparation_failed(self, submission_id: UUID, *, error_code: str) -> None:
        """End an unfinished preparation without exposing dependency details."""

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'failed', last_error_code = %s, updated_at = NOW()
                WHERE submission_id = %s AND state = 'preparing'
                """,
                (error_code, submission_id),
            )

    def get_for_credential(
        self, submission_id: UUID, *, credential_id: str
    ) -> GaslessSubmissionRecord:
        """Read one credential-owned record while applying expiry transitions.

        Credential scope is part of both update and select, which avoids leaking
        whether another insurer owns a guessed UUID. Stale preparation leases
        and unsigned deadlines become explicit terminal records before return.
        """

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = CASE
                        WHEN state = 'preparing' THEN 'failed'
                        ELSE 'expired'
                    END,
                    last_error_code = CASE
                        WHEN state = 'preparing' THEN 'preparation_lease_expired'
                        ELSE 'signature_deadline_expired'
                    END,
                    updated_at = NOW()
                WHERE submission_id = %s AND credential_id = %s
                  AND (
                    (state = 'preparing' AND updated_at < NOW() - INTERVAL '10 minutes')
                    OR (
                        state IN ('prepared', 'authorized')
                        AND deadline < EXTRACT(EPOCH FROM NOW())
                    )
                  )
                """,
                (submission_id, credential_id),
            )
            cursor.execute(
                """
                SELECT * FROM gasless_claim_submissions
                WHERE submission_id = %s AND credential_id = %s
                """,
                (submission_id, credential_id),
            )
            return self._record(cursor.fetchone())

    def authorize(
        self,
        submission_id: UUID,
        *,
        credential_id: str,
        signature: str,
        now: datetime,
    ) -> GaslessSubmissionRecord:
        """Persist a verified signature through an idempotent locked transition.

        The service verifies the signature on-chain before calling this method.
        This transaction enforces ownership, deadline, and state, accepts an
        exact replay, and rejects attempts to replace a recorded signature.
        """

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM gasless_claim_submissions
                WHERE submission_id = %s AND credential_id = %s
                FOR UPDATE
                """,
                (submission_id, credential_id),
            )
            record = self._record(cursor.fetchone())
            if record.state in {"authorized", "signed", "broadcast", "confirmed"}:
                if record.insurer_signature != signature:
                    raise GaslessSubmissionConflictError(
                        "A different signature is already recorded for this submission"
                    )
                return record
            if record.state != "prepared":
                raise GaslessSubmissionConflictError(
                    f"Submission in state {record.state!r} cannot be authorized"
                )
            if record.deadline is None or record.deadline < int(now.timestamp()):
                cursor.execute(
                    """
                    UPDATE gasless_claim_submissions
                    SET state = 'expired', updated_at = %s,
                        last_error_code = 'signature_deadline_expired'
                    WHERE submission_id = %s
                    """,
                    (now, submission_id),
                )
                raise GaslessSubmissionConflictError(
                    "The gasless signature deadline has expired"
                )
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'authorized', insurer_signature = %s,
                    authorized_at = %s, updated_at = %s,
                    last_error_code = NULL
                WHERE submission_id = %s
                RETURNING *
                """,
                (signature, now, now, submission_id),
            )
            return self._record(cursor.fetchone())

    def list_relay_candidates(
        self, *, limit: int = 20
    ) -> tuple[GaslessSubmissionRecord, ...]:
        """Return a bounded, deterministic batch of relayable outbox records.

        Expired unsigned authorizations are removed first. Signed records sort
        ahead of broadcasts and new authorizations so persist-before-broadcast
        recovery is serviced promptly without starving confirmation checks.
        """

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'expired', updated_at = NOW(),
                    last_error_code = 'signature_deadline_expired'
                WHERE state = 'authorized'
                  AND deadline < EXTRACT(EPOCH FROM NOW())
                """
            )
            cursor.execute(
                """
                SELECT * FROM gasless_claim_submissions
                WHERE state IN ('authorized', 'signed', 'broadcast')
                ORDER BY
                    CASE state WHEN 'signed' THEN 0 WHEN 'broadcast' THEN 1 ELSE 2 END,
                    created_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            return tuple(self._record(row) for row in cursor.fetchall())

    def persist_signed_transaction(
        self,
        submission_id: UUID,
        *,
        relayer_address: str,
        rpc_pending_nonce: int,
        sign: Callable[[int], SignedRelayTransaction],
    ) -> GaslessSubmissionRecord:
        """Atomically reserve a nonce and persist signed bytes before broadcast.

        An advisory lock serializes one chain-and-relayer nonce stream across
        workers. Allocation uses the greater of the RPC pending nonce and the
        database's next reservation. Both the latest submission view and the
        immutable first-attempt row commit in the same transaction.
        """

        with self.database.cursor() as cursor:
            scope = f"gasless-relayer:{self._chain_scope(submission_id, cursor)}:{relayer_address.lower()}"
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (scope,),
            )
            cursor.execute(
                "SELECT * FROM gasless_claim_submissions WHERE submission_id = %s FOR UPDATE",
                (submission_id,),
            )
            record = self._record(cursor.fetchone())
            if record.raw_transaction is not None:
                return record
            if record.state != "authorized":
                raise GaslessSubmissionConflictError(
                    f"Submission in state {record.state!r} cannot be signed for relay"
                )
            cursor.execute(
                """
                SELECT next_nonce FROM gasless_relayer_nonces
                WHERE chain_id = %s AND lower(relayer_address) = lower(%s)
                FOR UPDATE
                """,
                (record.chain_id, relayer_address),
            )
            nonce_row = cursor.fetchone()
            reserved_nonce = max(
                rpc_pending_nonce,
                int(nonce_row["next_nonce"]) if nonce_row is not None else 0,
            )
            signed = sign(reserved_nonce)
            if signed.nonce != reserved_nonce:
                raise GaslessSubmissionConflictError(
                    "Relay signer returned a transaction with the wrong nonce"
                )
            cursor.execute(
                """
                INSERT INTO gasless_relayer_nonces (
                    chain_id, relayer_address, next_nonce, updated_at
                ) VALUES (%s, lower(%s), %s, NOW())
                ON CONFLICT (chain_id, relayer_address) DO UPDATE SET
                    next_nonce = EXCLUDED.next_nonce,
                    updated_at = EXCLUDED.updated_at
                """,
                (record.chain_id, relayer_address, reserved_nonce + 1),
            )
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'signed', relayer_address = %s, relayer_nonce = %s,
                    raw_transaction = %s, transaction_hash = %s,
                    max_fee_per_gas = %s, max_priority_fee_per_gas = %s,
                    updated_at = NOW(), relay_attempts = relay_attempts + 1
                WHERE submission_id = %s
                RETURNING *
                """,
                (
                    relayer_address,
                    signed.nonce,
                    signed.raw_transaction,
                    signed.transaction_hash,
                    signed.max_fee_per_gas,
                    signed.max_priority_fee_per_gas,
                    submission_id,
                ),
            )
            record = self._record(cursor.fetchone())
            cursor.execute(
                """
                INSERT INTO gasless_relay_attempts (
                    submission_id, attempt_number, transaction_hash,
                    raw_transaction, relayer_nonce, max_fee_per_gas,
                    max_priority_fee_per_gas
                ) VALUES (%s, 1, %s, %s, %s, %s, %s)
                """,
                (
                    submission_id,
                    signed.transaction_hash,
                    signed.raw_transaction,
                    signed.nonce,
                    signed.max_fee_per_gas,
                    signed.max_priority_fee_per_gas,
                ),
            )
            return record

    @staticmethod
    def _chain_scope(submission_id: UUID, cursor) -> int:
        """Resolve the chain component used by the relayer advisory-lock scope."""

        cursor.execute(
            "SELECT chain_id FROM gasless_claim_submissions WHERE submission_id = %s",
            (submission_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise GaslessSubmissionNotFoundError("Gasless submission was not found")
        return int(row["chain_id"])

    def mark_broadcast(self, submission_id: UUID) -> GaslessSubmissionRecord:
        """Record a successful/replayed send without erasing its first send time.

        ``last_broadcast_at`` drives replacement timing while ``broadcast_at``
        preserves the first network attempt. A concurrent confirmation is
        treated as successful idempotent completion.
        """

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'broadcast', broadcast_at = COALESCE(broadcast_at, NOW()),
                    last_broadcast_at = NOW(), updated_at = NOW(),
                    last_error_code = NULL
                WHERE submission_id = %s AND state IN ('signed', 'broadcast')
                RETURNING *
                """,
                (submission_id,),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "SELECT * FROM gasless_claim_submissions WHERE submission_id = %s",
                    (submission_id,),
                )
                record = self._record(cursor.fetchone())
                if record.state == "confirmed":
                    return record
                raise GaslessSubmissionConflictError(
                    f"Submission in state {record.state!r} cannot be marked broadcast"
                )
            record = self._record(row)
            cursor.execute(
                """
                UPDATE gasless_relay_attempts
                SET broadcast_at = COALESCE(broadcast_at, NOW())
                WHERE submission_id = %s AND transaction_hash = %s
                """,
                (submission_id, record.transaction_hash),
            )
            return record

    def list_relay_transaction_hashes(self, submission_id: UUID) -> tuple[str, ...]:
        """Return every original/replacement hash that may have been mined.

        Newest-first lookup usually finds a replacement quickly, but retaining
        older hashes is essential because miners may include any same-nonce
        attempt that was propagated before the replacement.
        """

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                SELECT transaction_hash FROM gasless_relay_attempts
                WHERE submission_id = %s
                ORDER BY attempt_number DESC
                """,
                (submission_id,),
            )
            return tuple(str(row["transaction_hash"]) for row in cursor.fetchall())

    def persist_replacement_transaction(
        self,
        submission_id: UUID,
        *,
        sign: Callable[[int, int, int], SignedRelayTransaction],
    ) -> GaslessSubmissionRecord:
        """Persist a strictly higher-fee transaction for the same EOA nonce.

        Row locking makes concurrent replacement attempts converge on one set of
        signed bytes. Fee and nonce checks preserve Ethereum replacement rules;
        append-only attempt history keeps every potentially mined hash visible.
        """

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM gasless_claim_submissions
                WHERE submission_id = %s FOR UPDATE
                """,
                (submission_id,),
            )
            record = self._record(cursor.fetchone())
            # Another worker may already have created the replacement. Returning
            # its signed bytes lets both workers safely broadcast the same hash.
            if record.state == "signed":
                return record
            if (
                record.state != "broadcast"
                or record.relayer_nonce is None
                or record.max_fee_per_gas is None
                or record.max_priority_fee_per_gas is None
            ):
                raise GaslessSubmissionConflictError(
                    f"Submission in state {record.state!r} cannot be fee-bumped"
                )
            signed = sign(
                record.relayer_nonce,
                record.max_fee_per_gas,
                record.max_priority_fee_per_gas,
            )
            if signed.nonce != record.relayer_nonce:
                raise GaslessSubmissionConflictError(
                    "Replacement signer returned a transaction with the wrong nonce"
                )
            if (
                signed.max_fee_per_gas <= record.max_fee_per_gas
                or signed.max_priority_fee_per_gas <= record.max_priority_fee_per_gas
            ):
                raise GaslessSubmissionConflictError(
                    "Replacement transaction fees must increase"
                )
            attempt_number = record.relay_attempts + 1
            cursor.execute(
                """
                INSERT INTO gasless_relay_attempts (
                    submission_id, attempt_number, transaction_hash,
                    raw_transaction, relayer_nonce, max_fee_per_gas,
                    max_priority_fee_per_gas
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    submission_id,
                    attempt_number,
                    signed.transaction_hash,
                    signed.raw_transaction,
                    signed.nonce,
                    signed.max_fee_per_gas,
                    signed.max_priority_fee_per_gas,
                ),
            )
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'signed', raw_transaction = %s,
                    transaction_hash = %s, max_fee_per_gas = %s,
                    max_priority_fee_per_gas = %s, relay_attempts = %s,
                    updated_at = NOW(), last_error_code = NULL
                WHERE submission_id = %s
                RETURNING *
                """,
                (
                    signed.raw_transaction,
                    signed.transaction_hash,
                    signed.max_fee_per_gas,
                    signed.max_priority_fee_per_gas,
                    attempt_number,
                    submission_id,
                ),
            )
            return self._record(cursor.fetchone())

    def mark_confirmed(
        self,
        submission_id: UUID,
        *,
        transaction_hash: str,
        block_number: int,
        claim_id: int,
    ) -> GaslessSubmissionRecord:
        """Commit the exact verified receipt as the workflow's terminal success.

        Replaying the same claim ID, block, and hash is accepted. Different
        receipt facts conflict so a late or unrelated transaction cannot rewrite
        already confirmed application truth.
        """

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = 'confirmed', transaction_hash = %s,
                    block_number = %s, claim_id = %s, confirmed_at = NOW(),
                    updated_at = NOW(), last_error_code = NULL
                WHERE submission_id = %s AND state IN ('signed', 'broadcast')
                RETURNING *
                """,
                (transaction_hash, block_number, claim_id, submission_id),
            )
            row = cursor.fetchone()
            if row is not None:
                return self._record(row)
            cursor.execute(
                "SELECT * FROM gasless_claim_submissions WHERE submission_id = %s",
                (submission_id,),
            )
            record = self._record(cursor.fetchone())
            if record.state == "confirmed":
                if (
                    record.transaction_hash is not None
                    and record.transaction_hash.lower() == transaction_hash.lower()
                    and record.block_number == block_number
                    and record.claim_id == claim_id
                ):
                    return record
                raise GaslessSubmissionConflictError(
                    "Submission was confirmed with a different receipt"
                )
            raise GaslessSubmissionConflictError(
                f"Submission in state {record.state!r} cannot be confirmed"
            )

    def record_relay_error(
        self,
        submission_id: UUID,
        *,
        error_code: str,
        terminal: bool,
    ) -> None:
        """Persist a stable relay error code and optionally terminate the record.

        Retryable errors leave the current outbox state intact for a later poll.
        Terminal errors move it to ``failed``. Provider messages and signed raw
        transaction material are never copied into the public error field.
        """

        with self.database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gasless_claim_submissions
                SET state = CASE WHEN %s THEN 'failed' ELSE state END,
                    last_error_code = %s, updated_at = NOW()
                WHERE submission_id = %s
                  AND state IN ('authorized', 'signed', 'broadcast')
                """,
                (terminal, error_code, submission_id),
            )
