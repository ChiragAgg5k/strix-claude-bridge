# ADR-002: execution-loop ownership

Status: Accepted for experimental companion scope

## Context

Strix normally uses OpenAI Agents/LiteLLM, while Claude Agent SDK owns querying, tool dispatch, provider context, streaming, and cancellation.

## Decision

Claude Agent SDK is the sole model/tool-loop owner for this backend. Never nest it in `Runner.run_streamed` and never present it as a LiteLLM model. One bridge runtime owns one active SDK client/event pump per agent.

## Consequences

Provider-native state is authoritative in-process. Existing OpenAI/LiteLLM providers are untouched. Separate event/session/usage adapters are required, and exact live reconnect behavior remains unverified.

## Rejected

LiteLLM wrapper and nested agent loops because they duplicate history, tool authority, terminal state, budgets, and cancellation.
