"""Save-time workflow validation.

docs/architecture.md section 2.2, verbatim list:

    - Exactly one source node (the trigger: Manual or Webhook).
    - The graph is acyclic, and every node is reachable from the trigger.
    - Ports and node keys are valid and unique.
    - Node parameters validate against that node type's JSON Schema.
    - Referenced credentials exist and are of the correct type.
    - Expressions parse under the restricted grammar (section 8).

This validator is deliberately strict: an unknown node type, an edge to a
nonexistent port, or a credential/expression check it cannot perform is a
blocking error, never silently accepted (M1 task instructions: "Do not
create a permissive validator that silently accepts unknown node types or
invalid connections.").
"""

from __future__ import annotations

from flowforge.domain.graph import ExecutionMode, WorkflowDefinition
from flowforge.domain.validation import ValidationIssue, ValidationResult
from flowforge.engine.graph_utils import find_cycle, reachable_from
from flowforge.engine.ports import CredentialDirectory, ExpressionSyntaxChecker, NodeTypeCatalog


def _iter_strings(value: object) -> list[str]:
    """Recursively collect every string leaf in a (JSON-shaped) params
    value, in a deterministic order, so expression syntax checking covers
    nested dict/list structures without needing a schema to know where
    strings might be."""
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_iter_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_iter_strings(v))
    return out


def validate(
    definition: WorkflowDefinition,
    *,
    node_types: NodeTypeCatalog,
    credentials: CredentialDirectory,
    expressions: ExpressionSyntaxChecker,
) -> ValidationResult:
    """Validate ``definition``. Pure function; raises nothing for
    ordinary validation failures (they become ``ValidationIssue``s)."""
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    if not definition.nodes:
        errors.append(ValidationIssue("empty_workflow", "a workflow must have at least one node"))
        return ValidationResult(tuple(errors), tuple(warnings))

    # --- node keys already de-duplicated by WorkflowDefinition.__post_init__ ---

    # --- resolve node types first; everything else needs them ---
    specs = {}
    for node in definition.nodes:
        spec = node_types.get(node.type, node.type_version)
        if spec is None:
            errors.append(
                ValidationIssue(
                    "unknown_node_type",
                    f"no node type '{node.type}' version {node.type_version} is registered",
                    node_key=node.key,
                )
            )
        specs[node.key] = spec

    # --- exactly one source node, and it must be a trigger type ---
    source_keys = [n.key for n in definition.nodes if not definition.edges_into(n.key)]
    if len(source_keys) == 0:
        errors.append(ValidationIssue("no_source_node", "the workflow has no source node (every node has an incoming edge, or there are no nodes)"))
    elif len(source_keys) > 1:
        errors.append(
            ValidationIssue(
                "multiple_source_nodes",
                f"exactly one source node is required; found {len(source_keys)}: {source_keys}",
            )
        )
    else:
        source_key = source_keys[0]
        source_spec = specs.get(source_key)
        if source_spec is not None and not source_spec.is_trigger:
            errors.append(
                ValidationIssue(
                    "source_node_not_a_trigger",
                    f"the source node '{source_key}' is of type '{source_spec.type}', which is not a trigger",
                    node_key=source_key,
                )
            )

    # --- acyclic ---
    cycle = find_cycle(definition)
    if cycle is not None:
        errors.append(ValidationIssue("cycle_detected", f"the workflow graph contains a cycle: {' -> '.join(cycle)}"))

    # --- reachability from the (first, if multiple) source node ---
    if source_keys and cycle is None:
        reachable = reachable_from(definition, source_keys[0])
        for node in definition.nodes:
            if node.key not in reachable:
                errors.append(
                    ValidationIssue(
                        "unreachable_node",
                        f"node '{node.key}' is not reachable from the source node",
                        node_key=node.key,
                    )
                )

    # --- edges reference existing nodes and valid, declared ports ---
    node_keys = {n.key for n in definition.nodes}
    for edge in definition.edges:
        if edge.source_key not in node_keys:
            errors.append(ValidationIssue("edge_unknown_source_node", f"edge '{edge.id}' references unknown source node '{edge.source_key}'", edge_id=edge.id))
        if edge.target_key not in node_keys:
            errors.append(ValidationIssue("edge_unknown_target_node", f"edge '{edge.id}' references unknown target node '{edge.target_key}'", edge_id=edge.id))

        source_spec = specs.get(edge.source_key)
        if source_spec is not None and source_spec.output_port(edge.source_port) is None:
            errors.append(
                ValidationIssue(
                    "edge_unknown_source_port",
                    f"edge '{edge.id}' references undeclared output port '{edge.source_port}' on node '{edge.source_key}'",
                    edge_id=edge.id,
                )
            )
        target_spec = specs.get(edge.target_key)
        if target_spec is not None and target_spec.input_port(edge.target_port) is None:
            errors.append(
                ValidationIssue(
                    "edge_unknown_target_port",
                    f"edge '{edge.id}' references undeclared input port '{edge.target_port}' on node '{edge.target_key}'",
                    edge_id=edge.id,
                )
            )

    # --- per-node: params, credentials, expressions ---
    for node in definition.nodes:
        spec = specs.get(node.key)
        if spec is None:
            continue  # already reported as unknown_node_type; nothing further to check

        for param_error in spec.validate_params(dict(node.params)):
            errors.append(ValidationIssue("invalid_parameter", param_error, node_key=node.key))

        if node.execution_mode is ExecutionMode.PER_ITEM and not spec.supports_per_item:
            errors.append(
                ValidationIssue(
                    "per_item_not_supported",
                    f"node '{node.key}' is configured for per-item execution, but node "
                    f"type '{node.type}' version {node.type_version} does not support it",
                    node_key=node.key,
                )
            )

        if node.credential_id is not None:
            if not spec.credential_types:
                errors.append(
                    ValidationIssue(
                        "credential_not_applicable",
                        f"node '{node.key}' of type '{node.type}' does not accept a credential",
                        node_key=node.key,
                    )
                )
            else:
                matched = any(credentials.exists(node.credential_id, t) for t in spec.credential_types)
                if not matched:
                    errors.append(
                        ValidationIssue(
                            "credential_not_found",
                            f"credential '{node.credential_id}' does not exist or is not one of the types {list(spec.credential_types)} accepted by node '{node.key}'",
                            node_key=node.key,
                        )
                    )

        for text in _iter_strings(dict(node.params)):
            for expr_error in expressions.check(text):
                errors.append(ValidationIssue("invalid_expression_syntax", expr_error, node_key=node.key))

    return ValidationResult(tuple(errors), tuple(warnings))
