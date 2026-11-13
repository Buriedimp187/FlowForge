"""The Clock port.

M1 task instructions, section E: "No direct wall-clock dependency inside
deterministic domain logic." This module therefore defines only the
``Clock`` protocol and a ``FakeClock`` test double. There is deliberately
NO system-clock implementation here: wiring a real clock to live engine
code is a concern for the worker (M2), which also owns the database-clock
semantics described in docs/architecture.md section 4 ("All timestamps are
the database clock... never a worker's local clock"). Nothing in this
package should ever call ``time.time()`` directly.
"""

from __future__ import annotations

from typing import Protocol


class Clock(Protocol):
    """An injectable source of "now", in seconds."""

    def now(self) -> float: ...


class FakeClock:
    """A deterministic, manually-advanced clock for tests.

    Never used outside tests -- there is no default/real implementation
    in this package by design (see the module docstring).
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move a clock backwards")
        self._now += seconds

    def set(self, value: float) -> None:
        self._now = value
