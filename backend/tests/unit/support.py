"""Shared test fixtures: fake node-type specs and small sample workflows.

None of this is production code -- it exists only to give the unit tests
something concrete to validate/schedule/execute against, standing in for
the real node registry (M3) and credential store (M4).
"""

from __future__ import annotations

from flowforge.domain.effects import EffectClass
from flowforge.domain.graph import Edge, NodeDefinition, WorkflowDefinition
from flowforge.domain.node_types import NodeTypeSpec, ParamSpec
from flowforge.domain.ports import InputPort, OutputPort
from flowforge.engine.inmemory import (
    BalancedTemplateSyntaxChecker,
    InMemoryCredentialDirectory,
    InMemoryNodeTypeCatalog,
)

MANUAL_TRIGGER = NodeTypeSpec(
    type="manual_trigger",
    version=1,
    is_trigger=True,
    outputs=(OutputPort("main"),),
    effect_class=EffectClass.PURE,
)

WEBHOOK_TRIGGER = NodeTypeSpec(
    type="webhook_trigger",
    version=1,
    is_trigger=True,
    outputs=(OutputPort("main"),),
    effect_class=EffectClass.PURE,
)

SET_NODE = NodeTypeSpec(
    type="set",
    version=1,
    is_trigger=False,
    inputs=(InputPort("main", required=True),),
    outputs=(OutputPort("main"),),
    params=(ParamSpec("value", "string", required=False),),
    effect_class=EffectClass.PURE,
    supports_per_item=True,
)

IF_NODE = NodeTypeSpec(
    type="if",
    version=1,
    is_trigger=False,
    inputs=(InputPort("main", required=True),),
    outputs=(OutputPort("true"), OutputPort("false")),
    effect_class=EffectClass.PURE,
)

# Merge: one required port, one optional -- exactly the case
# docs/architecture.md section 3.2 describes for IF -> Merge rejoins.
MERGE_NODE = NodeTypeSpec(
    type="merge",
    version=1,
    is_trigger=False,
    inputs=(InputPort("required_in", required=True), InputPort("optional_in", required=False)),
    outputs=(OutputPort("main"),),
    effect_class=EffectClass.PURE,
)

HTTP_NODE = NodeTypeSpec(
    type="http_request",
    version=1,
    is_trigger=False,
    inputs=(InputPort("main", required=True),),
    outputs=(OutputPort("main"), OutputPort("error")),
    params=(ParamSpec("url", "string", required=True),),
    credential_types=("http_bearer",),
    effect_class=EffectClass.UNSAFE_WRITE,  # the conservative default (ADR-0005)
    supports_per_item=True,
)


def new_catalog() -> InMemoryNodeTypeCatalog:
    return InMemoryNodeTypeCatalog(
        (MANUAL_TRIGGER, WEBHOOK_TRIGGER, SET_NODE, IF_NODE, MERGE_NODE, HTTP_NODE)
    )


def new_credentials() -> InMemoryCredentialDirectory:
    creds = InMemoryCredentialDirectory()
    creds.register("cred-1", "http_bearer")
    return creds


def new_expression_checker() -> BalancedTemplateSyntaxChecker:
    return BalancedTemplateSyntaxChecker()


def trigger_node(key: str = "trigger") -> NodeDefinition:
    return NodeDefinition(key=key, type="manual_trigger", type_version=1, name="Trigger")


def set_node(key: str, value: str = "x") -> NodeDefinition:
    return NodeDefinition(key=key, type="set", type_version=1, params={"value": value})


def if_node(key: str) -> NodeDefinition:
    return NodeDefinition(key=key, type="if", type_version=1)


def merge_node(key: str) -> NodeDefinition:
    return NodeDefinition(key=key, type="merge", type_version=1)


def edge(id_: str, source_key: str, source_port: str, target_key: str, target_port: str) -> Edge:
    return Edge(id=id_, source_key=source_key, source_port=source_port, target_key=target_key, target_port=target_port)


def linear_workflow() -> WorkflowDefinition:
    """trigger --main--> set_a --main--> set_b"""
    return WorkflowDefinition(
        schema_version=1,
        nodes=(trigger_node(), set_node("set_a"), set_node("set_b")),
        edges=(
            edge("e1", "trigger", "main", "set_a", "main"),
            edge("e2", "set_a", "main", "set_b", "main"),
        ),
    )


def branching_merge_workflow() -> WorkflowDefinition:
    """trigger -> if -> (true -> set_true, false -> set_false) -> merge

    Merge's "required_in" is wired to set_true, "optional_in" to
    set_false -- so whichever branch the IF node takes, Merge becomes
    schedulable without ever requiring both branches at once.
    """
    return WorkflowDefinition(
        schema_version=1,
        nodes=(
            trigger_node(),
            if_node("branch"),
            set_node("set_true"),
            set_node("set_false"),
            merge_node("joiner"),
        ),
        edges=(
            edge("e1", "trigger", "main", "branch", "main"),
            edge("e2", "branch", "true", "set_true", "main"),
            edge("e3", "branch", "false", "set_false", "main"),
            edge("e4", "set_true", "main", "joiner", "required_in"),
            edge("e5", "set_false", "main", "joiner", "optional_in"),
        ),
    )
