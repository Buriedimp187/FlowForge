"""Lease heartbeat (docs/m2-persistence-design.md sections 3.1 and 4.7; architecture 4.4).

One dedicated thread per worker, with its own connection, renews the lease of
every run the worker owns, so a CPU-heavy or blocking node on the execution loop
cannot starve renewal (Risk R3). Each renewal is its own short, independently
committed transaction (a single ``run_fenced_guard``); no transaction is ever
held open between ticks or during node execution, and nothing database-shaped
crosses the thread boundary -- the heartbeat's ``LeaseProof`` dies with its
transaction. Consumers see only plain flags:

* ``cancel_requested`` -- sticky; set when a guard observes the cancel flag. The
  execution loop must then open its OWN new transaction and run a fresh guard
  before any cancellation-related write (section 4.7 steps 3-6).
* ``lost`` -- the database CONFIRMED the lease is gone (zero rows). Terminal: the
  run stops being renewed and the consumer must stop writing.
* ``state`` -- :class:`LeaseState`. A database error is NOT proof the lease is
  still valid (and not proof it is lost): it is recorded, retried on the next
  tick, and reported as ``UNCERTAIN`` until a renewal succeeds again. The same
  applies when no renewal has been confirmed within one ``ttl``. Consumers fail
  closed: they must not start new, unprotected external effects while the state
  is not ``HELD``.

All lease timestamps are database timestamps. The worker's monotonic clock is
used only to space ticks and to age the last confirmation.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from enum import Enum
from typing import Callable, Optional

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from flowforge.worker.claim import ClaimedRun
from flowforge.worker.connection import require_positive_interval
from flowforge.worker.guard import run_fenced_guard


class LeaseState(Enum):
    #: Confirmed by the claim or by a successful renewal within the last ``ttl``.
    HELD = "held"
    #: A renewal failed with a database error, or none was confirmed within ``ttl``.
    #: Ownership is unproven either way: fail closed.
    UNCERTAIN = "uncertain"
    #: The database confirmed the lease is gone (the guard matched zero rows).
    LOST = "lost"


class LeaseHandle:
    """What the execution loop sees of one run's lease. Thread-safe, read-only for consumers."""

    def __init__(self, claimed: ClaimedRun, ttl: timedelta, clock: Callable[[], float]) -> None:
        self.claimed = claimed
        self.lost = threading.Event()
        self.cancel_requested = threading.Event()
        self._ttl_seconds = ttl.total_seconds()
        self._clock = clock
        self._lock = threading.Lock()  # serializes a renewal with release()
        self._state_lock = threading.Lock()
        self._confirmed_at = clock()  # the claim that produced `claimed` just confirmed ownership
        self._failures = 0
        self._last_error: Optional[BaseException] = None
        self._released = False
        self._stopped = False  # the service stopped: nothing renews this lease any more

    @property
    def run_id(self) -> str:
        return self.claimed.run_id

    @property
    def state(self) -> LeaseState:
        if self.lost.is_set():
            return LeaseState.LOST
        with self._state_lock:
            if (
                self._stopped
                or self._failures > 0
                or self._clock() - self._confirmed_at > self._ttl_seconds
            ):
                return LeaseState.UNCERTAIN
        return LeaseState.HELD

    @property
    def is_confirmed(self) -> bool:
        """True only while ownership is confirmed; consumers fail closed otherwise."""
        return self.state is LeaseState.HELD

    @property
    def consecutive_failures(self) -> int:
        with self._state_lock:
            return self._failures

    @property
    def last_error(self) -> Optional[BaseException]:
        with self._state_lock:
            return self._last_error

    @property
    def released(self) -> bool:
        return self._released

    # -- written only by HeartbeatService --------------------------------------

    def _confirm(self) -> None:
        with self._state_lock:
            self._confirmed_at = self._clock()
            self._failures = 0
            self._last_error = None

    def _record_failure(self, error: BaseException) -> None:
        with self._state_lock:
            self._failures += 1
            self._last_error = error

    def _mark_stopped(self) -> None:
        with self._state_lock:
            self._stopped = True


class HeartbeatService:
    """One heartbeat thread per worker: a registry of owned runs, renewed in turn."""

    def __init__(
        self,
        engine: Engine,
        ttl: timedelta,
        period: Optional[timedelta] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = require_positive_interval("ttl", ttl)
        self.period = require_positive_interval("period", period if period is not None else ttl / 3)
        self._engine = engine
        self._clock = clock
        self._handles: dict[str, LeaseHandle] = {}
        # Guards the registry, `_stopping`, and thread creation, so that a concurrent
        # register()/start() can never slip in while stop() is sweeping the registry.
        self._registry_lock = threading.Lock()
        self._stopping = False
        self._tick_lock = threading.RLock()  # one pass at a time on the shared connection
        self._conn: Optional[Connection] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ticks = 0
        self._tick_cond = threading.Condition()
        self.last_error: Optional[BaseException] = None  # last unexpected service-level failure

    # -- registry ----------------------------------------------------------

    def register(self, claimed: ClaimedRun) -> LeaseHandle:
        """Start renewing ``claimed``'s lease. Call right after the claim commits.

        Allowed before :meth:`start` (the intended workflow) and while running;
        rejected once :meth:`stop` has been requested, so no handle is ever accepted
        without a heartbeat that can still renew it.
        """
        if not isinstance(claimed, ClaimedRun):
            raise TypeError(f"claimed must be a ClaimedRun, got {claimed!r}")
        handle = LeaseHandle(claimed, self.ttl, self._clock)
        with self._registry_lock:
            if self._stopping:
                raise RuntimeError("heartbeat service is stopped: cannot register a run for renewal")
            if claimed.run_id in self._handles:
                raise ValueError(f"run {claimed.run_id} is already registered for heartbeat")
            self._handles[claimed.run_id] = handle
        return handle

    def release(self, run_id: str) -> None:
        """Stop renewing a run (parked, finalized, or being released). Idempotent.

        Waits for a renewal of that run already in flight, so no renewal can start
        after this returns.
        """
        with self._registry_lock:
            handle = self._handles.pop(run_id, None)
        if handle is not None:
            with handle._lock:
                handle._released = True

    def handles(self) -> tuple[LeaseHandle, ...]:
        with self._registry_lock:
            return tuple(self._handles.values())

    # -- renewal -------------------------------------------------------------

    def tick(self) -> None:
        """One renewal pass: each owned run in its own short committed transaction."""
        with self._tick_lock:
            try:
                for handle in self.handles():
                    self._renew(handle)
                with self._tick_cond:
                    self._ticks += 1
                    self._tick_cond.notify_all()
            finally:
                if self._stopping:
                    # stop() never closes a connection another thread may be using; the
                    # tick that was using it closes it as soon as it is done.
                    self._drop_connection()

    def _renew(self, handle: LeaseHandle) -> None:
        with handle._lock:
            if handle._released or handle.lost.is_set():
                return
            try:
                conn = self._connection()
                with conn.begin():  # explicit, caller-owned, committed on exit
                    result = run_fenced_guard(conn, handle.claimed, self.ttl)
            except SQLAlchemyError as exc:
                # A database error proves nothing about ownership: record it, drop the
                # (possibly dead) connection, report UNCERTAIN, retry next tick.
                self._drop_connection()
                handle._record_failure(exc)
                return
            if result is None:  # confirmed: the lease is gone. Stop renewing; write nothing.
                handle.lost.set()
                with self._registry_lock:
                    self._handles.pop(handle.run_id, None)
                return
            if result.cancel_requested_at is not None:
                handle.cancel_requested.set()  # sticky; renewal continues while the run drains
            handle._confirm()

    def _connection(self) -> Connection:
        if self._conn is None:
            self._conn = self._engine.connect()
        return self._conn

    def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except SQLAlchemyError:
                pass

    # -- thread --------------------------------------------------------------

    @property
    def ticks(self) -> int:
        with self._tick_cond:
            return self._ticks

    def wait_for_ticks(self, count: int, timeout: float) -> bool:
        """Block until at least ``count`` renewal passes have completed."""
        with self._tick_cond:
            return self._tick_cond.wait_for(lambda: self._ticks >= count, timeout=timeout)

    def start(self) -> None:
        with self._registry_lock:
            if self._stopping:
                raise RuntimeError("heartbeat service is stopped and cannot be started")
            if self._thread is not None:
                raise RuntimeError("heartbeat already started")
            self._thread = threading.Thread(target=self._run, name="flowforge-heartbeat", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        period = self.period.total_seconds()
        try:
            while not self._stop.wait(period):
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - the thread must never die silently
                    self.last_error = exc
                    for handle in self.handles():
                        handle._record_failure(exc)
        finally:
            # The heartbeat thread owns its connection and closes it itself on exit.
            with self._tick_lock:
                self._drop_connection()

    def stop(self, timeout: float = 30.0) -> None:
        """Stop heartbeating; wait at most ``timeout`` seconds. Idempotent.

        Returns once the heartbeat thread has exited, having closed its own
        connection. Raises :class:`TimeoutError` if it is still alive after
        ``timeout`` (for example blocked in a database call): shutdown has then been
        *requested* and will complete when that call returns -- the thread exits and
        closes its own connection -- but it has not completed yet. Calling ``stop``
        again waits again. A service that was never started has no thread; any
        connection its manual ``tick()`` calls created is released here, but only
        when no tick is in flight (a connection is never closed from a second thread;
        an in-flight tick closes it itself when it ends).

        After the first call, :meth:`register` and :meth:`start` are rejected, every
        remaining handle reports ``UNCERTAIN`` (nothing renews it any more), and the
        registry is empty.
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValueError(f"timeout must be a non-negative number of seconds, got {timeout!r}")
        with self._registry_lock:
            self._stopping = True
            swept = list(self._handles.values())
            self._handles.clear()
            thread = self._thread
        for handle in swept:
            handle._mark_stopped()
        self._stop.set()

        if thread is not None:
            if thread is threading.current_thread():
                raise RuntimeError("stop() cannot be called from the heartbeat thread itself")
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError(
                    f"heartbeat thread still running after {timeout}s (a database call may be "
                    "blocked); it will exit and close its own connection when that call returns"
                )
            return  # the thread closed its own connection on exit

        if not self._tick_lock.acquire(timeout=timeout):
            raise TimeoutError(
                f"a manual tick() is still in flight after {timeout}s; it will close the "
                "connection itself when it finishes"
            )
        try:
            self._drop_connection()
        finally:
            self._tick_lock.release()

    def __enter__(self) -> "HeartbeatService":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
