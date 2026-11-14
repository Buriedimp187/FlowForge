"""Codec between persisted rows / JSONB columns and the pure domain objects.

Stage 1 of M2.5 (docs/m2-persistence-design.md sections 2, 4.3, 4.4). This module
has NO database dependency: it converts plain Python values (what the driver hands
back for a row) to and from :mod:`flowforge.domain` objects, so the pure engine can
read persisted state and the T2/T3 operations can bind JSONB parameters.

Rules (all explicit and deterministic):

* **Strict JSON.** Everything that becomes JSONB passes through the domain's own
  :func:`~flowforge.domain._immutable.deep_freeze` (None, bool, int, finite float,
  str, string-keyed mappings, lists) and is serialized by :func:`dumps_jsonb` with
  sorted keys, compact separators, UTF-8 characters kept, and ``NaN``/``Infinity``
  rejected. No parallel validator is introduced: domain constructors do the checking.
* **Storable in PostgreSQL jsonb.** Valid JSON is not enough: jsonb cannot hold U+0000
  (``DataError: unsupported Unicode escape sequence``) and a lone surrogate cannot be
  encoded as UTF-8 at all (``UnicodeEncodeError`` at bind time, which is not a database
  error). Both are rejected here, in object keys and in values, with a :class:`CodecError`
  naming the path, so a caller can turn them into a deterministic node-level outcome
  BEFORE any statement is sent. (What that outcome is, is a Stage 3 decision.)
* **jsonb numbers are not Python numbers** (PostgreSQL behavior, characterized by
  ``tests/integration/test_codec_jsonb.py``): jsonb stores exact decimals and prints
  them without an exponent. A float whose JSON text has a positive exponent (``1e+16``
  and above) is therefore read back as the Python ``int`` that text spells: equal to the
  original when the float is exactly that integer (``1e16``, ``1e22``), but NOT for a
  huge float such as ``1.5e+300``, whose binary double differs from ``15 * 10**299``.
  ``-0.0`` is read back as ``0.0``. Floats below ``1e16``, ``0.1 + 0.2``, ``5e-324`` and
  ints up to the 4300-digit string limit (beyond it ``dumps_jsonb`` raises) round-trip
  exactly. The codec does not mask this; no engine rule depends on the int/float
  distinction, and magnitudes above 1e16 are outside what node data is expected to carry.
* **Strict structure.** Persisted data is validated, never coerced: required fields
  must be present, strings must be strings, integers must be plain ``int`` (never
  ``bool`` or a float), and unknown fields are rejected. The domain constructors still
  perform their own validation on top.
* **Driver values are decoded exactly once.** The ``*_from_json`` functions take what the
  driver returns for a jsonb column: already-decoded Python values. A Python ``str`` there is
  a JSON string SCALAR, so it is rejected wherever an object or array is required (it is never
  parsed a second time, which would silently accept a malformed ``"{...}"`` string as an
  object). Raw JSON text is parsed only by the explicit, strict :func:`parse_json_text`.
* **Empty output is NULL.** ``node_runs.output`` is NULL when a node recorded no
  output; a port that emitted zero items is ``{"port": []}`` and stays distinct
  (architecture 3.2: "emitted on the port, even with zero items").
* **Timestamps.** ``node_runs.retry_at`` is ``timestamptz``; the domain holds epoch
  seconds as a float. Conversion goes through timezone-aware UTC datetimes, so the
  persisted precision is one microsecond. ``retry_at`` is always DATABASE-computed
  when written (design section 2): :func:`node_run_to_row` renders the row shape for
  fixtures and round trips, and the T2/T3 statements never bind it.
* **Skipped rows (Stage 3 obligation).** The database defaults ``node_runs.attempt`` to
  1, but the pure scheduler models a skipped node as ``attempt=0`` ("never started"; and
  ``failures=0``, ``started_epoch``/``effect_class`` NULL). The codec reads exactly what
  is stored and does not normalize, so the Stage 3 skip-row INSERT MUST write
  ``attempt = 0`` and ``failures = 0`` explicitly; otherwise a persisted skip reads back
  as one execution start.
* **``item_results``** is decoded and encoded here because the domain ``NodeRun`` models
  it, but no approved statement writes the column (design 4.4), so it is NULL for every
  persisted row until a design amendment says otherwise. See ``outcome.py``.
* **Errors.** ``node_runs.error`` is ``{"code", "message", "cause"}`` (the keys the
  design's SQL builds). Unknown keys in a stored error are ignored on read, since
  SQL-written errors may carry extra detail the domain has no field for.
* **Definitions.** ``workflow_versions.definition`` follows the architecture section
  2.1 shape (``nodes[]``, ``edges[]``; ``ui`` is stored and ignored); the schema
  version lives in its own column. Anything else at node/edge level is rejected so a
  field is never silently dropped. An edge without an ``id`` gets a deterministic one
  derived from its endpoints.

Malformed persisted data raises :class:`CodecError` (a ``ValueError``); the domain's
own validation errors are wrapped, not bypassed.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from flowforge.domain._immutable import deep_freeze, thaw
from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import (
    Edge,
    ExecutionMode,
    NodeDefinition,
    OnErrorPolicy,
    RetryPolicy,
    WorkflowDefinition,
)
from flowforge.domain.items import Item, ItemList
from flowforge.domain.node_run import ItemResult, NodeError, NodeRun
from flowforge.domain.states import NodeRunStatus, Outcome


class CodecError(ValueError):
    """Persisted data does not describe a valid domain object."""


# -- JSON ----------------------------------------------------------------------------


def _check_text(text: str, where: str) -> None:
    if "\x00" in text:
        raise CodecError(f"NUL character (U+0000) at {where}: PostgreSQL jsonb cannot store it")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise CodecError(f"lone surrogate at {where}: not encodable as UTF-8") from None


def _check_storable(value: Any, path: str = "$") -> None:
    """Reject strings (keys and values) that PostgreSQL jsonb cannot persist."""
    if isinstance(value, str):
        _check_text(value, path)
    elif isinstance(value, dict):
        for key, child in value.items():
            _check_text(key, f"{path} (object key {key!r})")
            _check_storable(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_storable(child, f"{path}[{index}]")


def to_json_value(value: Any) -> Any:
    """Strict JSON boundary, then jsonb storability; returns plain dict/list structure.

    ``TypeError``/``ValueError`` come from the domain's ``deep_freeze`` (not JSON);
    :class:`CodecError` means valid JSON that PostgreSQL jsonb cannot store.
    """
    plain = thaw(deep_freeze(value))
    _check_storable(plain)
    return plain


def dumps_jsonb(value: Any) -> str:
    """Deterministic JSON text for a JSONB parameter (``CAST(:p AS jsonb)``)."""
    return json.dumps(
        to_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name!r} is not allowed")


def parse_json_text(text: str) -> Any:
    """Parse RAW JSON TEXT (a file, a message, a test fixture) into Python values.

    This is the only place text is parsed. A value read from a jsonb column must NOT go through
    it: the driver has already decoded jsonb, so a Python ``str`` there is a JSON *string scalar*
    and is rejected by the ``*_from_json`` functions wherever an object or array is required.
    Parsing it again would silently turn a malformed ``"{\\"code\\": ...}"`` string into an object.

    Strict: ``NaN`` / ``Infinity`` / ``-Infinity`` (which Python's parser accepts) are refused.
    """
    if not isinstance(text, (str, bytes, bytearray)):
        raise CodecError(f"JSON text must be str or bytes, got {type(text).__name__}")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        raise CodecError(f"not valid JSON: {exc}") from exc


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CodecError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value


def _sequence(value: Any, what: str) -> list:
    if not isinstance(value, (list, tuple)):
        raise CodecError(f"{what} must be a JSON array, got {type(value).__name__}")
    return list(value)


def _only_keys(obj: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise CodecError(f"{what} has unknown field(s): {unknown}")


def _domain(what: str, build):  # type: ignore[no-untyped-def]
    """Run a domain constructor; wrap its validation errors so callers see one type."""
    try:
        return build()
    except CodecError:
        raise
    except (ValueError, TypeError) as exc:
        raise CodecError(f"invalid {what}: {exc}") from exc


def _plain_int(value: Any, what: str, *, minimum: Optional[int] = None) -> int:
    """A plain integer: never a bool, never a float, never a numeric string."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodecError(f"{what} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise CodecError(f"{what} must be >= {minimum}, got {value!r}")
    return value


def _string(value: Any, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise CodecError(f"{what} must be a string, got {value!r}")
    if not value and not allow_empty:
        raise CodecError(f"{what} must be a non-empty string")
    return value


def _optional_string(value: Any, what: str) -> Optional[str]:
    return None if value is None else _string(value, what)


def _boolean(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise CodecError(f"{what} must be a boolean, got {value!r}")
    return value


def _required(obj: Mapping[str, Any], names: tuple[str, ...], what: str) -> None:
    missing = [n for n in names if n not in obj]
    if missing:
        raise CodecError(f"{what} is missing required field(s): {missing}")


# -- timestamps ----------------------------------------------------------------------


def datetime_to_epoch(value: datetime) -> float:
    """A timezone-aware datetime as epoch seconds (naive datetimes are rejected)."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CodecError(f"expected a timezone-aware datetime, got {value!r}")
    return value.timestamp()


def epoch_to_datetime(seconds: float) -> datetime:
    """Epoch seconds as a UTC datetime (microsecond precision)."""
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


# -- items, outputs, errors, per-item results -------------------------------------------


def items_to_json(items: ItemList) -> list[dict[str, Any]]:
    return [{"json": to_json_value(item.json)} for item in items]


def items_from_json(raw: Any) -> ItemList:
    out = []
    for index, element in enumerate(_sequence(raw, "items")):
        obj = _mapping(element, f"item[{index}]")
        _only_keys(obj, frozenset({"json"}), f"item[{index}]")
        _required(obj, ("json",), f"item[{index}]")
        out.append(_domain(f"item[{index}]", lambda obj=obj: Item(json=_mapping(obj["json"], "item.json"))))
    return tuple(out)


def output_to_json(output: Mapping[str, ItemList]) -> Optional[dict[str, list[dict[str, Any]]]]:
    """``None`` (NULL) when nothing was emitted; otherwise ``{port: [items]}``."""
    if not output:
        return None
    return {port: items_to_json(items) for port, items in output.items()}


def output_from_json(raw: Any) -> dict[str, ItemList]:
    if raw is None:
        return {}
    return {port: items_from_json(items) for port, items in _mapping(raw, "output").items()}


def error_to_json(error: Optional[NodeError]) -> Optional[dict[str, Any]]:
    if error is None:
        return None
    return {"code": error.code, "message": error.message, "cause": error.cause}


def error_from_json(raw: Any) -> Optional[NodeError]:
    if raw is None:
        return None
    obj = _mapping(raw, "error")
    code = obj.get("code")
    if not isinstance(code, str) or not code:
        raise CodecError(f"error.code must be a non-empty string, got {code!r}")
    message, cause = obj.get("message", ""), obj.get("cause")
    if not isinstance(message, str):
        raise CodecError(f"error.message must be a string, got {message!r}")
    if cause is not None and not isinstance(cause, str):
        raise CodecError(f"error.cause must be a string or null, got {cause!r}")
    return NodeError(code=code, message=message, cause=cause)


def item_results_to_json(results: Optional[tuple[ItemResult, ...]]) -> Optional[list[dict[str, Any]]]:
    if results is None:
        return None
    return [
        {
            "index": r.index,
            "status": r.status,
            "output": None if r.output is None else items_to_json(r.output),
            "error": error_to_json(r.error),
        }
        for r in results
    ]


def item_results_from_json(raw: Any) -> Optional[tuple[ItemResult, ...]]:
    if raw is None:
        return None
    out = []
    for position, element in enumerate(_sequence(raw, "item_results")):
        obj = _mapping(element, f"item_results[{position}]")
        _only_keys(obj, frozenset({"index", "status", "output", "error"}), f"item_results[{position}]")
        _required(obj, ("index", "status"), f"item_results[{position}]")
        index = _plain_int(obj["index"], f"item_results[{position}].index", minimum=0)
        status = _string(obj["status"], f"item_results[{position}].status")
        output = obj.get("output")
        out.append(
            _domain(
                f"item_results[{position}]",
                lambda index=index, status=status, obj=obj, output=output: ItemResult(
                    index=index,
                    status=status,
                    output=None if output is None else items_from_json(output),
                    error=error_from_json(obj.get("error")),
                ),
            )
        )
    return tuple(out)


# -- node rows -------------------------------------------------------------------------

#: Columns of ``node_runs`` that :func:`node_run_from_row` reads.
NODE_RUN_COLUMNS = (
    "run_id", "node_key", "status", "attempt", "failures", "outcome", "effect_class",
    "started_epoch", "retry_at", "output", "error", "item_results",
)  # fmt: skip


def _enum(enum_type, value: Any, what: str):  # type: ignore[no-untyped-def]
    try:
        return enum_type(value)
    except (ValueError, TypeError):
        raise CodecError(f"{what} is not a valid {enum_type.__name__}: {value!r}") from None


def node_run_from_row(row: Mapping[str, Any]) -> NodeRun:
    """Build a :class:`NodeRun` from a ``node_runs`` row (driver values).

    ``started_at`` and ``resolved_params`` are persistence-only columns the domain
    does not model; they are not read.
    """
    missing = [c for c in NODE_RUN_COLUMNS if c not in row]
    if missing:
        raise CodecError(f"node_runs row is missing column(s): {missing}")
    retry_at = row["retry_at"]
    effect_class = row["effect_class"]
    run_id = row["run_id"]
    where = f"node_runs row ({run_id}, {row['node_key']})"
    run_id_text = str(run_id) if isinstance(run_id, uuid.UUID) else _string(run_id, f"{where}.run_id")
    node_key = _string(row["node_key"], f"{where}.node_key")
    attempt = _plain_int(row["attempt"], f"{where}.attempt", minimum=0)
    failures = _plain_int(row["failures"], f"{where}.failures", minimum=0)
    return _domain(
        where,
        lambda: NodeRun(
            run_id=run_id_text,
            node_key=node_key,
            status=_enum(NodeRunStatus, row["status"], "status"),
            attempt=attempt,
            failures=failures,
            effect_class=None if effect_class is None else _enum(EffectClass, effect_class, "effect_class"),
            outcome=_enum(Outcome, row["outcome"], "outcome"),
            retry_at=None if retry_at is None else datetime_to_epoch(retry_at),
            output=output_from_json(row["output"]),
            error=error_from_json(row["error"]),
            item_results=item_results_from_json(row["item_results"]),
            started_epoch=row["started_epoch"],
        ),
    )


def node_run_to_row(node_run: NodeRun) -> dict[str, Any]:
    """The row shape for ``node_run`` (JSON columns as plain JSON values).

    For fixtures and round trips. T2/T3 never bind ``retry_at`` from this: it is
    always computed by the database (``now() + interval``).
    """
    return {
        "run_id": node_run.run_id,
        "node_key": node_run.node_key,
        "status": node_run.status.value,
        "attempt": node_run.attempt,
        "failures": node_run.failures,
        "outcome": node_run.outcome.value,
        "effect_class": None if node_run.effect_class is None else node_run.effect_class.value,
        "started_epoch": node_run.started_epoch,
        "retry_at": None if node_run.retry_at is None else epoch_to_datetime(node_run.retry_at),
        "output": output_to_json(node_run.output),
        "error": error_to_json(node_run.error),
        "item_results": item_results_to_json(node_run.item_results),
    }


# -- workflow definitions ----------------------------------------------------------------

_NODE_KEYS = frozenset(
    {
        "key", "type", "type_version", "name", "params", "credential_id", "retry", "timeout_s",
        "on_error", "execution_mode", "continue_on_item_error",
    }
)  # fmt: skip
_EDGE_KEYS = frozenset({"id", "source_key", "source_port", "target_key", "target_port"})
_RETRY_KEYS = frozenset({"max_attempts", "backoff_base_s", "backoff_multiplier", "backoff_max_s"})
_DEFINITION_KEYS = frozenset({"nodes", "edges", "ui", "schema_version"})


def derive_edge_id(source_key: str, source_port: str, target_key: str, target_port: str) -> str:
    return f"{source_key}.{source_port}->{target_key}.{target_port}"


def _retry_from_json(raw: Any) -> Optional[RetryPolicy]:
    if raw is None:
        return None
    obj = _mapping(raw, "node.retry")
    _only_keys(obj, _RETRY_KEYS, "node.retry")
    return _domain("node.retry", lambda: RetryPolicy(**dict(obj)))


def _node_from_json(raw: Any, index: int) -> NodeDefinition:
    obj = _mapping(raw, f"nodes[{index}]")
    _only_keys(obj, _NODE_KEYS, f"nodes[{index}]")
    where = f"nodes[{index}]"
    _required(obj, ("key", "type", "type_version"), where)
    fields: dict[str, Any] = {
        "key": _string(obj["key"], f"{where}.key"),
        "type": _string(obj["type"], f"{where}.type"),
        "type_version": _plain_int(obj["type_version"], f"{where}.type_version", minimum=1),
        "name": _string(obj.get("name", ""), f"{where}.name", allow_empty=True),
        "credential_id": _optional_string(obj.get("credential_id"), f"{where}.credential_id"),
        "continue_on_item_error": _boolean(obj.get("continue_on_item_error", False), f"{where}.continue_on_item_error"),
    }
    timeout = obj.get("timeout_s")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise CodecError(f"{where}.timeout_s must be a number, got {timeout!r}")
        fields["timeout_s"] = timeout  # finiteness and > 0 are the domain's rules
    if "params" in obj:
        fields["params"] = _mapping(obj["params"], f"{where}.params")
    if obj.get("retry") is not None:
        fields["retry"] = _retry_from_json(obj["retry"])
    if "on_error" in obj:
        fields["on_error"] = _enum(OnErrorPolicy, obj["on_error"], "on_error")
    if "execution_mode" in obj:
        fields["execution_mode"] = _enum(ExecutionMode, obj["execution_mode"], "execution_mode")
    return _domain(f"nodes[{index}]", lambda: NodeDefinition(**fields))


def _edge_from_json(raw: Any, index: int) -> Edge:
    obj = _mapping(raw, f"edges[{index}]")
    _only_keys(obj, _EDGE_KEYS, f"edges[{index}]")
    ends = ("source_key", "source_port", "target_key", "target_port")
    if not all(isinstance(obj.get(k), str) for k in ends):
        raise CodecError(f"edges[{index}] needs string source_key, source_port, target_key, target_port")
    edge_id = _string(obj["id"], f"edges[{index}].id") if "id" in obj else derive_edge_id(*(obj[k] for k in ends))
    return _domain(f"edges[{index}]", lambda: Edge(id=edge_id, **{k: obj[k] for k in ends}))


def definition_from_json(raw: Any, schema_version: int) -> WorkflowDefinition:
    """Decode ``workflow_versions.definition`` using the row's ``schema_version``."""
    schema_version = _plain_int(schema_version, "schema_version", minimum=1)
    obj = _mapping(raw, "definition")
    _only_keys(obj, _DEFINITION_KEYS, "definition")
    if "schema_version" in obj:
        declared = _plain_int(obj["schema_version"], "definition.schema_version", minimum=1)
        if declared != schema_version:
            raise CodecError(f"definition.schema_version {declared!r} disagrees with the row's {schema_version!r}")
    nodes = tuple(_node_from_json(n, i) for i, n in enumerate(_sequence(obj.get("nodes", []), "definition.nodes")))
    edges = tuple(_edge_from_json(e, i) for i, e in enumerate(_sequence(obj.get("edges", []), "definition.edges")))
    return _domain("definition", lambda: WorkflowDefinition(schema_version=schema_version, nodes=nodes, edges=edges))


def definition_to_json(definition: WorkflowDefinition) -> dict[str, Any]:
    """Encode in the architecture 2.1 shape (every optional field explicit, edge ids kept)."""
    return {
        "nodes": [
            {
                "key": n.key,
                "type": n.type,
                "type_version": n.type_version,
                "name": n.name,
                "params": to_json_value(n.params),
                "credential_id": n.credential_id,
                "retry": None
                if n.retry is None
                else {
                    "max_attempts": n.retry.max_attempts,
                    "backoff_base_s": n.retry.backoff_base_s,
                    "backoff_multiplier": n.retry.backoff_multiplier,
                    "backoff_max_s": n.retry.backoff_max_s,
                },
                "timeout_s": n.timeout_s,
                "on_error": n.on_error.value,
                "execution_mode": n.execution_mode.value,
                "continue_on_item_error": n.continue_on_item_error,
            }
            for n in definition.nodes
        ],
        "edges": [
            {
                "id": e.id,
                "source_key": e.source_key,
                "source_port": e.source_port,
                "target_key": e.target_key,
                "target_port": e.target_port,
            }
            for e in definition.edges
        ],
    }
