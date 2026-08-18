# Architecture: version-pinned companion multi-agent backend

Status: experimental deterministic companion; Phases 1, 2, and 4–9 exit criteria remain incomplete. Baseline: Strix `1.5.3` (`8ede419dccf6742aa0e0c4fe3e7faf11c471ff9a`), OpenAI Agents `0.19.0`, Claude Agent SDK `0.2.139`.

## Decision

Claude Agent SDK owns the complete model/tool loop. It is not a LiteLLM model and is never nested in `Runner.run_streamed`. The companion package leaves Strix's existing provider path unchanged.

```text
scan CLI / target authorization
  -> one Strix sandbox + Caido bundle per scan
  -> Strix AgentCoordinator graph
     -> root AgentRuntime ---- one active ClaudeSDKClient + one strict SDK MCP server
     -> child AgentRuntime --- one active ClaudeSDKClient + one strict SDK MCP server
        -> adapted original Strix FunctionTools
        -> bound shared BaseSandboxSession / Caido / report state
  -> metadata transcript + usage + session checkpoint + tool journal
  -> Strix report state/writers -> JSON/CSV/Markdown/SARIF/final report
```

### Control flow

```text
User authorization -> CLI preflight (pins/auth-conflict/limits)
  -> target setup -> Docker/Caido creation -> root runtime
  -> SDK query -> MCP call -> adapted Strix FunctionTool -> Docker/coordinator/report state
  -> normalized result -> SDK -> terminal event/checkpoint -> Strix report writers
  -> SDK disconnect -> verified Docker deletion -> process exit
```

### Data flow

```text
Target/source --mounted--> Strix Docker --selected tool output--> Claude SDK/provider
User task/mailbox ------------------------------query-----------> Claude SDK/provider
Claude tool args --> strict MCP handler --> host-bound Strix context (identity not model supplied)
Finding tool --> intentional Strix reports (content) + metadata-only journal hashes
SDK messages --> sensitive stdout opt-in / omission-only durable mirror
SDK usage/model_usage --> zero-dollar bridge usage + Strix usage records
```

See [ownership boundaries](ownership-boundaries.md), [tool parity](tool-parity.md), [events/state](events-sessions-resume.md), and [security](security.md).

A scan-wide semaphore bounds active inference turns. `wait_for_agents` releases and then balances its permit while coordinator-blocked, so recursively-created children can run even at concurrency one. Total agents, cumulative turns, runtime, and a finite shared per-agent tool-call budget are separately bounded. Continuations reconnect to the same native provider session with only the remaining turn allowance because SDK options are immutable per transport.

## Coordination semantics

The bridge reuses Strix graph tools and coordinator identity. Root/child contexts bind `create_agent`, message, wait, stop, and lifecycle operations to the runner. Parent context inheritance is a bounded host-generated history snapshot, never provider-session reuse.

A coordinator subclass appends messages in memory and atomically snapshots only mailbox audit metadata/content hashes before scheduling an SDK interrupt. The runner acknowledges the in-memory mailbox only after `continue_with()` accepts the replacement query. One task owns each client, query sequence, event receiver, and disconnect. Child terminal handling always calls Strix's claim-once `notify_parent_on_terminal`; a successful `agent_finish` completion report and fallback failure/cancellation notice compete for the same exact-one slot.

`finish_scan` and `agent_finish` are successful lifecycle barriers. The SDK `PostToolUse` hook asks the provider loop to stop only after the Strix tool returned the expected success key. An intentional resulting abort is normalized to completion.

## Session authority and disabled restart

Claude native sessions are inference authority. JSONL and legacy viewer projections are mirrors only. Owner-only checkpoint audits record backend/SDK versions, model, tool-schema digest, provider-session SHA-256, SDK working-directory identity, cumulative settled turns, and journal state. Raw provider IDs remain in memory only for same-process continuation.

Process-restart resume is disabled. The coordinator graph cannot yet be hydrated atomically, and MCP handlers do not receive a stable provider tool-use ID. A fingerprint of `(agent, tool, arguments)` cannot distinguish provider replay from a later intentional identical action. Consequently the CLI rejects `--resume-token` before target or Docker side effects and emits no raw resume capability. The write-ahead journal persists only agent/tool/invocation identity, argument/result hashes, state, and error status. It never persists arguments/results/mailbox text and supports manual reconciliation after interruption, not automatic restart replay.

## Events and accounting

Backend events cover assistant text, user messages, tool call/result, partial, system, unknown provider frames, rate-limit, provider error, terminal, sandbox, and lifecycle state. Content-bearing events are sensitive. The default owner-only mirror stores metadata/omission markers and deduplicates exact event envelopes. `to_strix_stream_event` projects text/tool/result into legacy `raw_response_event` and `run_item_stream_event` shapes where feasible; this is not full Go TUI protocol parity.

Subscription accounting records requests, provider `model_usage` keys when available (configured selector only as fallback), input/output/cache tokens, and `auth_mode=claude_subscription`. It invents no default model identity. Provider dollar-cost fields are discarded and persisted cost is zero. The existing Strix API dollar budget is not used. Report dedupe is deterministic exact matching while this backend is active so report creation cannot silently invoke LiteLLM.

## Security invariants

1. Only SDK-created in-process MCP servers and their exact generated allowlists are accepted. Claude built-ins, filesystem settings, skills, external MCP, and project configuration are disabled.
2. Agent identity, sandbox, Caido, coordinator, mounts, credentials, and host paths are closure-bound authority, not model arguments.
3. All offensive shell/filesystem actions use the shared Strix Docker sandbox. Claude credentials and Docker socket are never mounted.
4. Every target requires explicit user authorization for live execution. Tests use only the bundled intentionally vulnerable fixture.
5. Bridge code never implements login or reads/copies/persists OAuth material. Every live user authenticates locally through official Anthropic tooling.
6. Sensitive stdout is opt-in; owner-only omission/hash metadata is the durable default. Provider session IDs are in-memory execution authority; durable state stores hashes only, and no resume capability is emitted while restart is disabled.
7. Shutdown order is: reject new children/tools, interrupt/settle clients, disconnect SDK subprocesses, close mirrors/report state, then delete the Strix sandbox.
8. Existing OpenAI/LiteLLM execution remains untouched.

## Companion and upstream constraints

The companion obtains Strix's generated `Filesystem`/`Shell` tools by binding capability clones to the real sandbox. It also uses pinned private lifecycle state to verify partial and final sandbox deletion. These are version-gated compatibility seams, not stable public Strix APIs.

A production upstream integration should add explicit backend dispatch, backend-neutral sandbox FunctionTools, coordinator interrupt handles, normalized event/transcript sinks, and provider checkpoint storage directly in Strix. The companion `py3-none-any` wheel does not modify existing Strix PyInstaller builds. The Claude SDK's large platform-specific bundled executable still needs collection, permission, signing/notarization, and smoke tests on macOS arm64/x86_64, Linux arm64/x86_64, and Windows x86_64.

## Rejected alternatives and tradeoffs

| Alternative | Why rejected | Accepted tradeoff |
|---|---|---|
| LiteLLM/provider wrapper or nested `Runner.run_streamed` | Both SDKs would own tool loops, history, cancellation, and terminal state. | A separate companion CLI until Strix has backend dispatch. |
| Reconstruct Claude context from Strix SQLite | Loses provider-owned hidden/session/tool identity. | Native SDK session is in-process authority; SQLite/TUI parity deferred. |
| Fingerprint replay after restart | Cannot distinguish replay from a later intentional identical action. | Restart disabled; metadata hashes support manual audit only. |
| Hosted login or pooled subscription | Crosses credential/policy/user-capacity boundaries. | Every eligible user must authenticate locally through official tooling. |
| Vendored Strix fork | Broad maintenance and publication surface. | Pinned private seams fail closed and need upstream work. |
| Reimplement Strix tools | Schema/behavior/security drift. | Adapt original FunctionTools and accept version coupling. |

Decision records are indexed in [docs/decisions](decisions/README.md).

## Remaining gates

Approved live evidence is still required for subscription authentication/organization, model entitlement, provider MCP routing, hook/interruption order, service limit errors, live root/child behavior, and autonomous scan quality. A safe process-restart design and tests are also required before resume can be re-enabled. Current Anthropic policy approval, license selection, five-platform packaging, and independent security/correctness review remain release blockers.
