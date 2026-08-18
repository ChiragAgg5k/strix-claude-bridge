# ADR-006: subscription usage accounting

Status: Accepted for experimental companion scope

## Context

Claude subscription capacity is not Anthropic API dollar spend. SDK terminal messages can report tokens/cache and `model_usage` identities, while the configured model selector can be absent.

## Decision

Record requests, input/output/cache tokens, provider-reported model IDs when available, otherwise a configured selector, and explicit auth mode. Invent no default model. Attribute each record to its real agent ID/name. Drop provider cost and persist zero dollars; do not apply Strix API dollar budget.

## Consequences

Host turn/runtime/concurrency/tool controls remain meaningful. Live field accuracy, quota/rate semantics, and account limits remain unverified. Multiple provider model IDs are retained in bridge aggregate state; Strix per-agent usage uses a single observed model only when unambiguous.

## Rejected

Converting subscription use to fake dollars, hardcoding a default model, and labeling all child usage as root.
