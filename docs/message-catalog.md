# Message Catalog: LLM Usage and Billing Wording

Status: **Normative, v3.4.** This document exists because the architecture requires the
exact wording of these three strings to be preserved verbatim, and requires that no
other code path render billing-related text. It is documentation only; no UI, frontend
string catalog, or i18n infrastructure has been implemented in M0.

## Background

A response from an LLM provider can report token usage without that usage proving a
financial charge occurred (free tiers, credits, quota accounting, and provider-specific
billing rules all vary, and FlowForge has no visibility into them). FlowForge therefore:

- never infers a financial charge from a successful response or from reported usage,
- never computes a monetary amount (no price tables, no currency, anywhere),
- and limits itself to three mutually exclusive, precisely worded UI states.

See `docs/architecture.md` §9 (AI provider abstraction and BYOK) and
`docs/adr/0013-ai-provider-port-and-byok.md` for the adapter contract that produces the
values driving this catalog.

## The three catalog entries (exact wording, do not alter)

| Catalog id | Exact wording |
|---|---|
| `llm.usage_reported` | "Usage reported by the provider: N tokens. This is a lower bound across attempts." |
| `llm.not_charged` | "Not charged (basis: <pre-dispatch \| provider documentation: ref>)." |
| `llm.billing_uncertain` | "Billing could not be determined. The provider may have charged for this request." |

`N` and `<...>` are template placeholders filled at render time; the surrounding text is
fixed and must not be paraphrased, abbreviated, or otherwise altered when implemented.

## Selection and precedence (per node execution attempt)

1. **`llm.not_charged`** — shown only when the adapter's `Disposition.billing_uncertain`
   is `false`, which is only ever produced alongside a non-empty
   `billing_basis` (`"pre_dispatch"` or `"provider_documented:<rule_id>"`). The
   `<basis>` placeholder renders that value (for `provider_documented`, including the
   dated documentation reference recorded against that rule).
2. **`llm.usage_reported`** — shown when `usage_reported` is non-null (the provider
   returned parsable token-usage data), and state 1 does not apply. Reported usage
   never implies a charge; this string makes no claim about billing at all.
3. **`llm.billing_uncertain`** — the default, shown otherwise: no usage was reported,
   and no `billing_basis` was established.

Per-node display aggregates across attempts: reported usage sums as a lower bound
across attempts; a run that had any `llm.billing_uncertain` attempt keeps that caveat
visible even if a later attempt also reported usage or was marked not-charged.

## Allowlist rule (catalog-id scope)

**Only these three catalog ids may render text that discusses usage or billing.** No
other string in the application — error messages, logs, tooltips, documentation pages
other than `docs/llm-usage-and-billing.md` (to be written when the LLM node is
implemented, M6) — may contain billing-related claims about a specific execution.

- `llm.usage_reported` must never contain any of: `charge`, `bill`, `cost`, `price`
  (in any casing/substring form). This was corrected in v3.4 specifically because the
  v3.3 wording violated this rule by including the word "charged"; the v3.4 wording
  above is the corrected, compliant text.
- `llm.not_charged` is the only id permitted to assert that no charge occurred, and only
  when rendered with its `billing_basis` populated.
- `llm.billing_uncertain` is the only id permitted to raise the possibility of a charge
  having occurred.
- No catalog id, and no other code path anywhere in the system, computes or displays a
  monetary amount, currency, or estimated cost.

## Required tests (to be implemented alongside the LLM node and its adapters, M6)

These are specified here so the requirement is not lost before implementation begins;
none of them exist as code yet.

1. **Exact-string regression test** for each of the three ids above, failing on any
   deviation from the wording in this document.
2. **Banned-term test** on `llm.usage_reported`: asserts the rendered string contains
   none of `charge`, `bill`, `cost`, `price`.
3. **Non-empty-basis test**: `llm.not_charged` cannot be selected/rendered unless
   `billing_basis` is non-empty; attempting to do so is a test failure (defensive
   assertion in the renderer, not just a UI convention).
4. **Catalog-id allowlist test**: a static/structural check that no other message id in
   the application's string catalog contains any of `charge`, `bill`, `cost`, `price`
   unless it is one of the three ids above.
5. **No-monetary-computation test**: a repository-wide check (e.g. forbidding currency
   symbols, `Decimal` money types, or price-table lookups in the `ai/` module) that
   fails if a monetary computation is introduced.
6. **Adapter conformance tests** (per provider adapter, M6): table-driven tests feeding
   representative provider responses through `classify()`, asserting
   `billing_uncertain` defaults to `true` for any unlisted status/response and is only
   `false` when a cited, dated rule applies.
7. **Scenario test** ("processed-then-dropped"): a fake provider that processes and
   would-bill a request, then drops the connection before any response — asserting the
   node surfaces `llm.billing_uncertain` (or, on an opted-in retry, a duplicate-charge
   warning), never `llm.not_charged`, and never a false "no charge" claim.

See `docs/risks-and-release-gates.md`, gate **RB8**, which makes items 1–4 and 6–7
release-blocking for v0.1.
