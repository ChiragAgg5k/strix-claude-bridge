# ADR-004: metadata state and disabled process restart

Status: Accepted for experimental companion scope

## Context

Exactly-once side effects cannot be inferred after process death without stable provider tool-use identity and complete coordinator restoration. Argument fingerprints cannot distinguish replay from a later intentional action. Raw tool/mailbox persistence leaks target secrets.

## Decision

Disable `--resume-token` before side effects. Emit no resume capability. Persist only owner-only checkpoint metadata with a provider-session hash, plus journal/graph task/invocation/mailbox hashes, state, and minimal metadata; reject checkpoint resume at composition boundaries. Never replay completed tool results.

## Consequences

Operators must manually reconcile `started` entries and begin a fresh run. Raw output/mailbox recovery is impossible by design. Safe restart remains deferred until graph hydration, stable identity, dedupe isolation, and true process tests exist.

## Rejected

Fingerprint result replay, automatic ambiguous side-effect retry, and reconstructing Claude context from Strix SQLite.
