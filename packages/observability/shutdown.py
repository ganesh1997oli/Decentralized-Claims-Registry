"""Let Docker stop the long-running processes without cutting work in half."""

from __future__ import annotations

import signal
import threading
from types import FrameType

from .logging import get_event_logger

logger = get_event_logger(__name__)


class ShutdownSignal:
    """Turn SIGINT and SIGTERM into a small flag that loops can check safely.

    Docker sends SIGTERM during a normal restart or deployment. The default
    Python behaviour can end a process immediately, which might skip Kafka or
    producer cleanup. Installing this helper lets the active poll finish and
    then closes network clients in the function's existing ``finally`` block.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self) -> None:
        """Register handlers in the process's main thread."""

        signal.signal(signal.SIGINT, self._request_stop)
        signal.signal(signal.SIGTERM, self._request_stop)

    def _request_stop(
        self,
        signum: int,
        _frame: FrameType | None,
    ) -> None:
        """Remember the request; do not perform unsafe cleanup in the handler."""

        signal_name = signal.Signals(signum).name
        self._event.set()
        logger.info("shutdown.requested", signal=signal_name)

    def is_set(self) -> bool:
        """Return ``True`` after Docker or a person asks the process to stop."""

        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Wait for a delay but wake immediately when shutdown is requested."""

        return self._event.wait(timeout)
