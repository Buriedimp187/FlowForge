"""Graceful shutdown primitives (docs/m2-persistence-design.md section 4.11, D-C/D-H).

SIGTERM is not run-deadline expiry and introduces no interruption cause; no node
result is fabricated. This module provides the pieces; the process entrypoint and
the loop that waits out the grace period belong to later stages.

1. :class:`ShutdownCoordinator` -- a signal-safe flag. ``should_claim()`` turns
   false once shutdown is requested (step 1: stop claiming).
2. :func:`release_owned_runs` -- step 3, applied at grace expiry to each owned run:
   stop its heartbeat, then park it with ``park_for_shutdown`` (``visible_at =
   now()``). A node row still ``running`` is left untouched; the next claim makes
   it an orphan resolved as a crash. ``recovery_count`` is not incremented.
"""

from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Iterable, Iterator, Optional

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from flowforge.worker.claim import ClaimedRun
from flowforge.worker.connection import require_positive_interval, worker_connection
from flowforge.worker.heartbeat import HeartbeatService
from flowforge.worker.park import ParkResult, park_for_shutdown


class ShutdownCoordinator:
    """Process-wide shutdown flag. ``request_shutdown`` only sets an Event (signal-safe)."""

    def __init__(self) -> None:
        self._requested = threading.Event()

    def request_shutdown(self) -> None:
        self._requested.set()

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def should_claim(self) -> bool:
        """False once shutdown is requested: the worker stops claiming new runs."""
        return not self._requested.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until shutdown is requested (or ``timeout`` seconds pass)."""
        return self._requested.wait(timeout)

    @contextmanager
    def installed(self, signals: Iterable[int] = (signal.SIGTERM,)) -> Iterator["ShutdownCoordinator"]:
        """Route ``signals`` to :meth:`request_shutdown`; restore the old handlers on exit.

        Must be entered from the main thread (a Python restriction on
        ``signal.signal``).
        """

        def handler(signum: int, frame: object) -> None:
            self.request_shutdown()

        previous = {sig: signal.signal(sig, handler) for sig in signals}
        try:
            yield self
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)


@dataclass(frozen=True)
class ReleaseOutcome:
    run_id: str
    #: ``None`` only when a database error stopped this run's release; the run is then
    #: simply left for lease expiry and crash recovery.
    result: Optional[ParkResult]
    error: Optional[BaseException] = None


def release_owned_runs(
    engine: Engine,
    owned: Iterable[ClaimedRun],
    ttl: timedelta,
    has_live_attempts: Callable[[ClaimedRun], bool],
    heartbeat: Optional[HeartbeatService] = None,
) -> list[ReleaseOutcome]:
    """Gracefully release every owned run at grace expiry (design section 4.11 step 3).

    Per run, on its own clean connection: stop its heartbeat first (so a renewal
    cannot race the park and report a spurious lease loss), then park. A lease lost
    meanwhile is reported as ``LEASE_LOST``, never raised; a database error on one
    run is recorded and does not stop the others.
    """
    require_positive_interval("ttl", ttl)
    outcomes: list[ReleaseOutcome] = []
    for claimed in owned:
        if heartbeat is not None:
            heartbeat.release(claimed.run_id)
        try:
            live = bool(has_live_attempts(claimed))
            with worker_connection(engine) as conn:
                result = park_for_shutdown(conn, claimed, ttl, live_attempts=live)
            outcomes.append(ReleaseOutcome(claimed.run_id, result))
        except SQLAlchemyError as exc:
            outcomes.append(ReleaseOutcome(claimed.run_id, None, exc))
    return outcomes
