# Live Claude Team verification

Date: 2026-08-18. This evidence used the official local Claude tooling and `claude-agent-sdk==0.2.139`. No credential file or credential value was inspected, copied, logged, or persisted. All documented API/cloud override selectors were unset in each inference subprocess. Prompts were harmless compatibility probes, not security tests against an external target.

## Active official login

`claude auth status --json`, executed through Claude Code `2.1.234`, reported:

- `loggedIn: true`;
- `authMethod: claude.ai`;
- `apiProvider: firstParty`;
- an active organization was reported; its name is intentionally withheld;
- `subscriptionType: team`.

The email address, organization name, and organization identifier are intentionally omitted from public artifacts. This is live technical evidence for an active local Team login and organization selection. It is not evidence that Anthropic approves this third-party bridge, that the organization has approved every security-testing use, or that the bridge can enforce organization selection.

## Harmless SDK and MCP probe

A one-turn SDK/MCP verification run was executed headlessly with the SDK default model selection and one strict in-process `sandbox_exec` tool. Observed results:

- SDK connected and emitted init/status, streaming, assistant, tool, user, rate-limit, result, and disconnect frames;
- Claude selected `sandbox_exec` exactly once;
- the network-disabled Alpine container executed the requested `printf` command with exit code `0`, 15 stdout bytes, no stderr, and no truncation;
- the result completed successfully after two turns;
- `ResultMessage.model_usage` identified `claude-opus-5[1m]`, canonical model `claude-opus-5`, provider `firstParty`, and a 1,000,000-token context window;
- usage included input, output, cache creation, and cache-read token fields;
- the bridge disconnected the SDK and verified sandbox cleanup.

One `RateLimitEvent` and nine `api_retry` status frames occurred after the tool result. The request still completed after about 226 seconds. This demonstrates retry/event behavior, not a stable latency or quota guarantee.

## Concurrency, cancellation, and timeout probes

Two headless one-turn probes were started concurrently. Both completed successfully through the first-party provider, used the same default model identity, disconnected, and removed their sandboxes. Each emitted a `RateLimitEvent` but no API retry. This verifies concurrency two for this account at that moment only; it does not establish a maximum or future capacity guarantee.

A programmatic cancellation event was set during a connected live query. The bridge emitted `cancellation_requested` and `cancelled`, invoked SDK interruption, disconnected, and removed the sandbox.

A separate live MCP probe requested `sleep 2` with a 0.5-second command timeout. Exactly one tool call produced a sanitized `tool_error`; the model session then completed, disconnected, and removed the sandbox. This verifies live MCP timeout/error recovery, not a hard whole-process timeout guarantee.

## Full live Strix root/child scan

After the harmless probes, the Team-backed bridge scanned the bundled intentionally vulnerable local fixture through the production Strix sandbox. An initial successful run targeted the authorized repository fixture and exposed the writable-mount risk documented below. After the fixture was restored and recovery was restricted to proven wait interruptions, the final confirmation used a disposable copy and exited `0` with `completed=true` and `simulated_inference=false`; the repository fixture and disposable copy had identical final SHA-256 digests.

Observed live path:

- the root created and coordinated one child through the real Strix agent graph;
- the final SDK run selected 19 Strix tool calls across `root0001` and `agent002`;
- the child confirmed one CWE-22 path traversal and completed through `agent_finish`;
- the root completed through `finish_scan` after same-process mailbox-interruption recovery;
- one vulnerability, Markdown report, JSON, CSV, SARIF 2.1.0, run record, usage record, and agent graph were written;
- three rate-status frames reported `status=allowed`; overage was unavailable, but subscription execution remained permitted;
- SDK clients disconnected and Strix sandbox deletion was verified;
- default JSONL contained no `session_id` key and durable state retained only provider-session audit hashes.

The first two live scan attempts found integration defects before the successful confirmation runs:

1. A normal `RateLimitEvent(status=allowed)` was incorrectly treated as a terminal plan limit. Normalization now retains only bounded rate metadata, drops provider event/session identifiers and raw provider data, and aborts immediately only for `status=rejected`.
2. A root waiting for its child received the SDK terminal reason `aborted_tools` when mailbox delivery interrupted the active wait. The coordinator now treats that exact reason as a bounded, same-process recovery case; other provider errors still fail closed.

Regression tests cover both paths. One pre-fix metadata-only temporary JSONL did contain a raw provider session identifier in a rate event. It remained owner-only outside the repository, was not copied into documentation, and the final live run verified the repaired omission contract.

The live child used Strix's `apply_patch` capability and changed its bind-mounted fixture. The fixture was restored, and README/demo commands now require or create a disposable copy. Users must assume a scanned local target can be modified by Strix shell/patch tools.

## What remains unverified

- Written Anthropic approval or current policy eligibility for distributing third-party subscription-backed software.
- Organization-level cyber approval for arbitrary targets or data transfer to Anthropic.
- Bridge-side enforcement of the active organization; official status was observed, not cryptographically bound to a run.
- Maximum Team quota, rate limits, long-running concurrency, capacity-reset behavior, and model availability over time.
- Autonomous security quality beyond the bundled fixture and exhaustive browser/Caido/web paths.
- Process-restart resume, full Strix TUI/SQLite integration, and five-platform release packaging.

The final raw probe/scan JSONL remained in owner-only temporary files outside the repository. Default output omitted raw prompts, commands, command output, credentials, identity fields, and provider session IDs. Intentional findings and reports contained fixture security evidence.
