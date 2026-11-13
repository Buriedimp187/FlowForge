"""Validation issues and results for save-time workflow validation.

docs/architecture.md section 2.2. "Distinguish blocking errors from
non-blocking warnings" (M1 task instructions) -- ``ValidationResult``
keeps the two in separate tuples rather than one flat list with a severity
flag, so a caller cannot accidentally treat a warning as blocking by
forgetting to filter.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationIssue:
    """One validation finding, blocking or not.

    ``code`` is a stable, machine-checkable identifier (snake_case), so
    tests assert on ``code`` rather than on free-text ``message`` wording.
    """

    code: str
    message: str
    node_key: str | None = None
    edge_id: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating a :class:`WorkflowDefinition`.

    Deterministic: both tuples preserve the order issues were discovered
    in, which is itself deterministic because the validator walks nodes
    and edges in definition order (never via set/dict iteration).
    """

    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def error_codes(self) -> tuple[str, ...]:
        return tuple(e.code for e in self.errors)

    def warning_codes(self) -> tuple[str, ...]:
        return tuple(w.code for w in self.warnings)
