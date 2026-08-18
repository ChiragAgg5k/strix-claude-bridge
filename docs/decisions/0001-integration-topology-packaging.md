# ADR-001: integration topology and packaging

Status: Accepted for experimental companion scope

## Context

Native Strix has no reviewed dispatch seam for an SDK that owns an agent loop. Vendoring/forking Strix or patching installed code would widen maintenance and publication risk.

## Decision

Ship a version-pinned `strix-claude-bridge` companion CLI and `py3-none-any` package. Reuse Strix setup/sandbox/tools/coordinator/reports without modifying its existing CLI/providers.

## Consequences

Existing Strix remains unchanged, but native `STRIX_AGENT_BACKEND` selection is deferred and Phase 4 is incomplete. The SDK’s platform executable is not solved by a pure-Python wheel; five-platform collection, permissions, signing/notarization, and smoke tests block release.

## Rejected

Vendored fork, runtime monkey-patch as product interface, and claiming the companion is a native backend.
