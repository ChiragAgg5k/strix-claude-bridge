# Compatibility and evidence categories

Report date: 2026-08-18. Baseline: bridge `0.1.0`, Python `3.13.12`, Claude Agent SDK `0.2.139`, Strix `1.5.3`, OpenAI Agents `0.19.0`, Strix source `8ede419dccf6742aa0e0c4fe3e7faf11c471ff9a`.

Evidence categories are intentionally separate. A unit/mock or scripted result is never described as live Claude evidence.

## Unit / mock / scripted SDK evidence

| Capability | Observed evidence | Limit |
|---|---|---|
| SDK option isolation and exact MCP allowlist | backend/tool tests | No provider transport. |
| SDK message/event/result normalization | pinned dataclass fixtures | Exact live frames can differ. |
| `model_usage` identity and selector fallback | result/runtime tests; absent default remains unknown | Live field population unverified. |
| Remaining-turn reconnect (8+3) | scripted clients receive allowances 10 then 2 | Live native reconnect behavior unverified. |
| Timeout/cancel/cleanup errors | deterministic fake clients | Live subprocess timing unverified. |
| Shared 500 tool cap and parked waits | adapter + runner tests | Live subscription capacity unverified. |
| Root/child graph and exact-one terminal notice | scripted root/child clients | Autonomous tool selection unverified. |
| Usage ID/name attribution | generated `run.json` assertions | Provider token accuracy unverified. |
| Journal/graph non-disclosure | sentinel credentials/output/request/mailbox tests | SHA-256 can confirm guessed low-entropy values. |
| Restart guard | CLI/config rejection before side effects | Resume is absent, not complete. |
| Context-window error | scripted terminal reason | Not actual context pressure. |

## Real Docker evidence

| Capability | Observed evidence | Limit |
|---|---|---|
| Hardened Alpine probe execution/cleanup | opt-in Docker tests and CLI probe | Not the production Strix image. |
| Bounded 1,000,000-byte output | capture 1,024 bytes, producer stopped, exit 137 | One transport chunk can be transiently allocated. |
| Anonymous volume cleanup | locally built `VOLUME` image test | Daemon failure can still block cleanup. |
| Real Strix sandbox single-agent fixture | opt-in `test_single_agent_docker.py` | Inference is scripted. |
| Container cleanup | labeled-container and Strix private lifecycle checks | Production Docker/Caido is not multi-tenant hardening. |

## Real Strix, credential-free evidence

| Capability | Observed evidence | Limit |
|---|---|---|
| Pinned runtime compatibility | installed metadata and exact study commit checks | Other versions fail closed. |
| Effective root/child tools | generated 33-per-role [inventory](docs/tool-inventory.json) | Optional/future skills/tools require regeneration. |
| Original FunctionTool handlers | SDK MCP handlers invoke Strix tools with bound context | Browser/Caido/web paths not exhaustive. |
| Root/child fixture scan | real coordinator, sandbox, report state; scripted SDK clients | No Claude autonomy. |
| Concurrency one/two | child completes while root wait is parked | Account concurrency not measured. |
| Tool failure recovery | failed shell result followed by completed report | Scripted provider response. |
| Findings/reports/SARIF | JSON/CSV/Markdown/final/SARIF 2.1.0 with one fixture result | SARIF can use synthetic location when upstream data lacks one. |
| Owner-only artifacts | `0700` run tree and `0600` files asserted | Operator still controls retention/export. |
| Real agent usage labels | `root0001/Root Agent`, `agent002/Fixture Specialist` | Default model remains unknown when result omits `model_usage`. |
| Metadata-only journal/graph | no raw tool/mailbox content in persisted files | Intentional reports remain content-bearing. |

## Live Claude evidence

Harmless probes and a full authorized root/child fixture scan were run on 2026-08-18. Exact sanitized observations, defects found, fixes, and limits are recorded in [live verification](docs/live-verification.md).

| Capability | Observed live evidence | Limit |
|---|---|---|
| Team login and active organization | Official `claude auth status --json`: first-party `claude.ai`, active organization present (identity withheld), subscription type `team` | No bridge-side organization enforcement or policy/cyber approval. |
| Headless SDK query and strict custom MCP | Default-model probe selected only `sandbox_exec`; Docker command exited 0 and session completed | Isolated compatibility probe only. |
| Full Strix root/child scan | Live root created one child; 19 model-selected tool calls; one CWE-22 finding; Markdown/JSON/CSV/SARIF; exit 0 | Bundled authorized disposable fixture only; writable mounts remain an operator risk. |
| Live coordinator interruption | `aborted_tools` from an interrupted root wait recovered in-process and reached `finish_scan` | Exact same-process reason only; restart resume remains disabled. |
| Live privacy repair | Final default JSONL had no `session_id`; rate frames retain bounded status only | A pre-fix owner-only temp event exposed a raw provider ID and triggered the regression fix. |
| Event/model/usage shape | Init/status/stream/tool/rate/result frames; `claude-opus-5[1m]`, first-party provider, token/cache fields | One SDK/account/version observation; fields and models can change. |
| Concurrent sessions | Two parallel one-turn probes both completed and cleaned up | Verifies concurrency two only at that moment, not maximum capacity. |
| Cancellation and cleanup | Connected query interrupted programmatically; SDK disconnected and sandbox closed | Does not prove rollback of already completed side effects. |
| MCP command timeout | `sleep 2` under a 0.5-second timeout produced one sanitized tool error; session recovered and cleaned up | Tool timeout only, not a whole-process hard deadline. |
| Rate behavior | Rate-limit events were observable; one two-turn probe retried nine times and eventually succeeded | Quota ceilings, reset behavior, plan limits, and stable latency remain unknown. |

Still unverified and unclaimed: written/current policy approval, organization-level authorization for arbitrary targets, autonomous security quality beyond the bundled fixture, exhaustive browser/Caido tools, full Go TUI/local-viewer and SQLite behavior, long-running account limits, five-platform packaging, and process-restart resume (explicitly disabled).

## Capability status

| Capability | Status |
|---|---|
| Tool schema/context/result/error/image/timeout bridge | Deterministic scope implemented. |
| Companion execution session abstraction | Implemented; not native Strix selection. |
| Fresh root/child coordination | Deterministic scope and one full live root/child fixture scan implemented. |
| Events | Partial legacy projection; full TUI/SQLite deferred. |
| Restart resume/replay | Deferred and rejected before side effects. |
| Usage/limits | Deterministic implementation; live basic usage/concurrency/retry evidence, maximum limits unknown. |
| Auth conflict rejection | Implemented; active Team login/org observed live, bridge-side org enforcement and policy attestation blocked. |
| Security logging defaults | Metadata/omission/hash default implemented; live rate-event identifier leak repaired and regression-tested. |
| Packaging | Wheel/sdist built; five-platform executable release blocked. |
| Publication | Blocked by policy, license, packaging, native-integration decision, and post-live review gates. |

See [implementation status](docs/implementation-status.md) for every `plan.md` criterion, [ownership](docs/ownership-boundaries.md) for unchanged/adapted layers, and [security](docs/security.md) for residual risks.

## Credential-free validation ladder

Final evidence must include locked resolution/sync, Ruff format/lint, deterministic tests, all opt-in Docker tests, generated tool inventory/doc checks, package build/metadata, root/scan help, normal/bounded sandbox probes, stable single/multi-agent scans, report/SARIF/redaction/permission inspection, zero residual labeled containers, no repository run outputs, and zero staged files. The implementation handoff records exact commands and exits.

## Policy and publication gate

The official Agent SDK overview was live-checked on 2026-08-18 and says third-party developers may not offer Claude.ai login or rate limits for products—including Agent SDK agents—unless previously approved. Therefore public subscription-backed release is blocked pending written Anthropic approval; see the [policy gate](docs/policy-gate.md). An alternative public design must use documented API-key/cloud-provider authentication and undergo a fresh review.

Target-owner approval must include provider transfer of selected source/request/mailbox/tool/finding data. Hosted login, credential pooling/extraction, private endpoint emulation, and subscription proxying remain non-goals regardless of authentication mode.

No LICENSE is present. Native backend selection, safe restart, full TUI/SQLite parity, five-platform executable collection/signing/smoke, licensing, and public release remain deferred or blocked.
