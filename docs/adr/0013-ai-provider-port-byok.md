# ADR-0013: AI provider port and BYOK

**Status:** Accepted (revised — final, v3.4 revision; see history below)

## Context

FlowForge integrates LLM providers under a Bring-Your-Own-Key model, with zero
telemetry and no managed billing. The abstraction must stay small, avoid heavy
dependencies, and be honest about what it can and cannot know regarding whether a
given request was actually charged by the provider.

## Decision

A narrow port — `complete(request) -> {text, usage, finish_reason, raw}` — with thin
adapters (OpenAI-compatible/Ollama, Anthropic, Gemini) built over the same audited
outbound HTTP client as every other request (ADR-0012). **No LangChain or LiteLLM
dependency.** Keys live only in the credential store (ADR-0010); the LLM node holds
a credential reference, never an embedded key. No cumulative spend cap exists in
v0.1, and **FlowForge never computes a monetary amount anywhere in the system.**

**Adapter billing-classification contract** (the final, corrected form):

```
classify(outcome) -> Disposition {
   retryable: bool,
   usage_reported: { input_tokens?, output_tokens?, total_tokens? } | null,
   billing_uncertain: bool,
   billing_basis: "pre_dispatch" | "provider_documented:<rule_id>" | "none",
}
```

- `usage_reported` holds **only** what the provider's response actually returned.
  **A successful response with reported usage does not, by itself, establish that a
  financial charge occurred** — FlowForge never infers a charge from a status code
  or from usage alone.
- `billing_uncertain = false` is permitted only alongside a non-empty
  `billing_basis`: a transport fact (`pre_dispatch` — the dispatch marker shows no
  bytes left the process) or a specific, dated, cited provider-documentation rule
  (`provider_documented:<rule_id>`). Everything else defaults to
  `billing_uncertain = true`, including any unlisted status/response and any
  successful response whose usage was merely reported.
- **No provider-independent billing guarantee exists anywhere in this contract.**
  4xx is generally treated as a permanent request failure, but billing certainty is
  never inferred from the status code alone; 429 is retried only if that specific
  provider's documented semantics say so.
- The three resulting UI states, and their exact wording, are specified in
  `docs/message-catalog.md` and are release-blocking (gate RB8).

## Consequences

Small, auditable adapter code, each provider's quirks handled explicitly rather than
through a heavy shared library, at the cost of possible duplicate LLM charges after
a crash-triggered automatic retry of a `read`-class node (Risk R11) and the ongoing
maintenance cost of keeping each adapter's cited billing rules current against that
provider's documentation (Risk R13).

## Revision history

Originally, `read`-class HTTP semantics (4xx generally unbilled, 429 generally
retryable) were applied uninspected to LLM responses; this was corrected once it was
identified that billing behavior is a provider-specific business rule, not an HTTP
semantic, and the `classify()` contract with mandatory citations was introduced.
Separately, the UI wording initially implied a charge from a successful response's
reported usage ("billed: N tokens, lower bound") — corrected to the current
"usage reported, not a charge claim" wording after a self-contradiction was found
between that string and the no-billing-term rule it was meant to satisfy (see
`docs/message-catalog.md` for the exact fix).

## Related

`docs/architecture.md` §9; `docs/message-catalog.md`; ADR-0005, ADR-0012.
