"""Drain authorized ERC-2771 submissions from PostgreSQL into Ethereum.

The worker is deliberately separate from FastAPI. The API never receives the
relayer key, and HTTP retries never allocate EOA nonces. PostgreSQL persists raw
signed transactions before broadcast so a crash can safely replay the same
bytes and transaction hash.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime

from apps.backend.app.gasless_blockchain import (
    GaslessBlockchainError,
    GaslessRelayChain,
)
from packages.integrations.postgres import (
    GaslessSubmissionRecord,
    PostgresDatabase,
    PostgresGaslessSubmissionRepository,
    PostgresStorageError,
)
from packages.observability import (
    ShutdownSignal,
    configure_logging,
    get_event_logger,
)

logger = get_event_logger(__name__)


class GaslessRelayWorker:
    """Move each durable relay request through sign, broadcast, and confirm."""

    def __init__(
        self,
        *,
        store: PostgresGaslessSubmissionRepository,
        chain: GaslessRelayChain,
        confirmation_blocks: int,
        stuck_transaction_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Configure confirmation and replacement policy for one worker.

        ``confirmation_blocks`` controls when a mined receipt becomes durable
        application truth. ``stuck_transaction_seconds`` controls when the same
        nonce may be re-signed with higher fees; it does not create a second
        logical claim.
        """

        if confirmation_blocks < 0:
            raise ValueError("confirmation_blocks cannot be negative")
        if stuck_transaction_seconds < 1:
            raise ValueError("stuck_transaction_seconds must be positive")
        self.store = store
        self.chain = chain
        self.confirmation_blocks = confirmation_blocks
        self.stuck_transaction_seconds = stuck_transaction_seconds
        self.clock = clock

    def _broadcast(self, record: GaslessSubmissionRecord) -> GaslessSubmissionRecord:
        """Replay the persisted raw bytes, then durably mark their broadcast.

        Signing and persistence happen earlier. A crash between Ethereum and
        PostgreSQL can therefore rebroadcast the identical bytes/hash safely.
        """

        if not record.raw_transaction or not record.transaction_hash:
            raise GaslessBlockchainError("Persisted relay transaction is incomplete")
        self.chain.broadcast(record.raw_transaction, record.transaction_hash)
        current = self.store.mark_broadcast(record.submission_id)
        logger.info(
            "gasless.relay_broadcast",
            submission_id=str(current.submission_id),
            transaction_hash=current.transaction_hash,
            relayer_nonce=current.relayer_nonce,
            relay_attempts=current.relay_attempts,
        )
        return current

    def _is_stuck(self, record: GaslessSubmissionRecord) -> bool:
        """Decide whether the newest broadcast is old enough for a fee bump."""

        broadcast_at = record.last_broadcast_at or record.broadcast_at
        if broadcast_at is None:
            return True
        if broadcast_at.tzinfo is None:
            raise GaslessBlockchainError(
                "Persisted relay broadcast timestamp is not timezone-aware"
            )
        return (
            self.clock() - broadcast_at
        ).total_seconds() >= self.stuck_transaction_seconds

    def _receipt_for_any_attempt(self, record: GaslessSubmissionRecord):
        """Find a receipt across the original hash and every replacement hash.

        Ethereum may mine any transaction sharing the nonce. Checking the full
        attempt history prevents the worker from overlooking an older attempt
        after a higher-fee replacement has been prepared.
        """

        for transaction_hash in self.store.list_relay_transaction_hashes(
            record.submission_id
        ):
            receipt = self.chain.receipt(transaction_hash)
            if receipt is not None:
                return receipt
        return None

    @staticmethod
    def _failure(exc: GaslessBlockchainError) -> tuple[str, bool]:
        """Map detailed failures to stable, non-sensitive operational codes.

        Terminal errors prove that this logical request cannot safely continue.
        Dependency, fee, and nonce-conflict errors remain retryable/operator
        visible because changing infrastructure state may resolve them. Raw RPC
        text is deliberately not persisted or returned to the browser.
        """

        message = str(exc).lower()
        terminal_markers = {
            "invalid, expired, or uses a stale nonce": "authorization_invalid",
            "reverted on-chain": "relay_reverted",
            "did not emit exactly one": "receipt_event_missing",
            "does not match": "receipt_event_mismatch",
            "target does not match": "deployment_mismatch",
            "exceeds the configured gas cap": "transaction_gas_cap_exceeded",
        }
        for marker, code in terminal_markers.items():
            if marker in message:
                return code, True
        if "network fees exceed" in message:
            return "fee_cap_exceeded", False
        if "nonce was consumed" in message:
            return "relayer_nonce_conflict", False
        return "relay_dependency_unavailable", False

    def process(self, record: GaslessSubmissionRecord) -> None:
        """Advance one durable record through every immediately safe transition.

        The normal path is ``authorized -> signed -> broadcast -> confirmed``.
        Signed bytes are saved before network I/O, all known receipts are checked
        before replacement, and replacements reuse the EOA nonce with higher
        capped fees. Each step is safe when another worker or restart repeats it.
        """

        current = record
        try:
            if current.state == "authorized":
                sign = self.chain.prepare_relay_signer(current)
                pending_nonce = self.chain.pending_nonce()
                current = self.store.persist_signed_transaction(
                    current.submission_id,
                    relayer_address=self.chain.account.address,
                    rpc_pending_nonce=pending_nonce,
                    sign=sign,
                )

            if current.state not in {"signed", "broadcast"}:
                return
            receipt = self._receipt_for_any_attempt(current)
            if receipt is not None and current.state == "signed":
                # Ethereum can accept and mine persisted bytes before a crash
                # prevents PostgreSQL from recording ``broadcast``. Repair the
                # durable transition from the receipt evidence before confirming
                # so the state machine remains auditable without rebroadcasting.
                current = self.store.mark_broadcast(current.submission_id)
            if receipt is None and current.state == "signed":
                if not current.insurer_signature:
                    raise GaslessBlockchainError(
                        "Persisted relay authorization is incomplete"
                    )
                # A worker restart can find signed bytes after the forward
                # deadline. Reverify before spending gas, while checking all
                # known receipts first covers a crash after successful broadcast.
                self.chain.verify_signature(current, current.insurer_signature)
                current = self._broadcast(current)
                if current.state == "confirmed":
                    return
                receipt = self._receipt_for_any_attempt(current)
            if receipt is None:
                if self._is_stuck(current):
                    if (
                        current.max_fee_per_gas is None
                        or current.max_priority_fee_per_gas is None
                    ):
                        raise GaslessBlockchainError(
                            "Persisted relay fee quote is incomplete"
                        )
                    sign = self.chain.prepare_relay_signer(
                        current,
                        minimum_max_fee_per_gas=current.max_fee_per_gas,
                        minimum_priority_fee_per_gas=(current.max_priority_fee_per_gas),
                    )
                    current = self.store.persist_replacement_transaction(
                        current.submission_id,
                        sign=lambda nonce, _previous_max_fee, _previous_priority: sign(
                            nonce
                        ),
                    )
                    if current.state == "signed":
                        self._broadcast(current)
                return
            if not self.chain.has_confirmations(receipt, self.confirmation_blocks):
                return
            result = self.chain.confirm(current, receipt)
            confirmed = self.store.mark_confirmed(
                current.submission_id,
                transaction_hash=result.transaction_hash,
                block_number=result.block_number,
                claim_id=result.claim_id,
            )
            logger.info(
                "gasless.relay_confirmed",
                submission_id=str(confirmed.submission_id),
                claim_id=confirmed.claim_id,
                transaction_hash=confirmed.transaction_hash,
                block_number=confirmed.block_number,
            )
        except GaslessBlockchainError as exc:
            code, terminal = self._failure(exc)
            self.store.record_relay_error(
                current.submission_id,
                error_code=code,
                terminal=terminal,
            )
            logger.warning(
                "gasless.relay_failed",
                submission_id=str(current.submission_id),
                error_code=code,
                terminal=terminal,
                exception_type=type(exc).__name__,
            )

    def run_once(self, *, limit: int = 20) -> int:
        """Process one bounded database batch and return the number inspected.

        A bounded batch prevents a large backlog from monopolizing one poll and
        gives waiting-for-confirmation records regular opportunities to advance.
        """

        candidates = self.store.list_relay_candidates(limit=limit)
        for record in candidates:
            self.process(record)
        return len(candidates)


def _non_negative_int(name: str, default: int) -> int:
    """Read a zero-or-greater worker policy value or terminate startup clearly."""

    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise SystemExit(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise SystemExit(f"{name} must be a non-negative integer")
    return value


def main() -> None:
    """Run the isolated relay poll loop until SIGINT or SIGTERM is received.

    Startup validates database access, deployment bytecode, role separation,
    key source, and sponsorship caps before logging the payer address. Polling
    dependency failures are logged and retried without terminating the worker.
    """

    configure_logging("claims-gasless-relayer")
    shutdown = ShutdownSignal()
    shutdown.install()
    poll_seconds = _non_negative_int("GASLESS_RELAY_POLL_SECONDS", 2)
    if poll_seconds == 0:
        poll_seconds = 1
    try:
        worker = GaslessRelayWorker(
            store=PostgresGaslessSubmissionRepository(PostgresDatabase.from_env()),
            chain=GaslessRelayChain.from_env(),
            confirmation_blocks=_non_negative_int("CONFIRMATION_BLOCKS", 12),
            stuck_transaction_seconds=_non_negative_int(
                "GASLESS_STUCK_TRANSACTION_SECONDS", 120
            ),
        )
    except (GaslessBlockchainError, PostgresStorageError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    logger.info(
        "gasless.relayer_started",
        relayer_address=worker.chain.account.address,
        chain_id=worker.chain.deployment.chain_id,
        contract_address=worker.chain.deployment.address,
        forwarder_address=worker.chain.deployment.forwarder_address,
        confirmation_blocks=worker.confirmation_blocks,
    )
    while not shutdown.is_set():
        try:
            worker.run_once()
        except PostgresStorageError as exc:
            logger.warning(
                "gasless.relayer_poll_failed",
                exception_type=type(exc).__name__,
            )
        # Waiting-for-confirmation rows remain candidates. Always back off so a
        # healthy pending transaction cannot create a database/RPC busy loop.
        shutdown.wait(poll_seconds)


if __name__ == "__main__":
    main()
