# ADR-003: Strix tool bridge

Status: Accepted for experimental companion scope

## Context

Reimplementing pentest tools would drift from Strix schemas, sandbox authority, report semantics, and fixes. Claude SDK requires MCP tools.

## Decision

Deep-copy each effective pinned Strix FunctionTool schema into an SDK-created strict in-process MCP server. Closure-bind agent/coordinator/sandbox/Caido/report authority and invoke the original handler with pinned `ToolContext`.

## Consequences

Business logic and Docker routing remain Strix-owned. Errors/images/timeouts/cancellation need normalization. Dynamic approval/enablement fails closed. The generated inventory is a compatibility gate. Browser/Caido/provider-autonomy parity remains live-unverified.

## Rejected

External MCP, Claude built-in shell/filesystem, model-supplied authority, and duplicated tool implementations.
