# ADR-005: event translation and mirrors

Status: Accepted for experimental companion scope

## Context

Claude SDK message frames differ from Strix/OpenAI Agents events and the Go TUI protocol. Content can contain target secrets.

## Decision

Normalize SDK frames into versioned bridge events; mark content-bearing events sensitive. Persist omission-only metadata by default and provide limited text/tool/result legacy projections. Never use the mirror as inference authority.

## Consequences

CLI JSONL supports diagnostics, with explicit sensitive opt-in risk. Provider session IDs stay out of events. Full Go TUI/local-viewer and SQLite parity are deferred; exact live message variants need approved evidence.

## Rejected

Persisting full traffic by default, treating JSONL as resumable conversation state, and claiming generic projection is full protocol parity.
