# Security and privacy

## Threat model

Protected assets include Claude credentials/session IDs, target source and secrets, HTTP requests, command output, findings, host filesystem, Docker daemon, and subscription capacity. Potential adversaries are malicious target content, prompt injection, an overreaching model/tool call, accidental log upload, another local user, and an unauthorized operator. This is a single-user local experimental tool—not a hostile-code or multi-tenant security boundary.

## Authorization and policy

Live scan requires `--experimental --authorized-use`. This is operator confirmation, not proof. The user owns:

- target ownership/assessment authorization;
- approval to transfer selected target data to Anthropic;
- current Anthropic third-party/subscription policy determination;
- approved-organization and eligible-plan attestation;
- retention, licensing, and publication decisions.

An official live status check identified an active first-party Team organization on 2026-08-18; its identity is withheld from public artifacts. The repository has no written Anthropic approval, organization-level cyber approval, or bridge-side organization enforcement. Anthropic's official Agent SDK overview says public third-party Claude.ai login/rate-limit use requires prior approval, so public release is blocked; see the [policy gate](policy-gate.md).

## Authentication and precedence

Official Claude tooling/Agent SDK owns login. The bridge never implements login or reads/copies OAuth files. Before target preparation it rejects presence of known alternate selectors: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`, `AWS_BEARER_TOKEN_BEDROCK`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, and `CLAUDE_CODE_USE_FOUNDRY`. Values are never read or printed.

Absence of those variables does **not** prove subscription authentication, organization, entitlement, or policy eligibility. A separate official status check and harmless live probe supplied one-time technical evidence; see [live verification](live-verification.md). Claude credentials and Docker socket are not mounted into the scan container. The trusted SDK subprocess may still inherit unrelated host environment variables because the pinned SDK has no bridge-controlled replace-environment seam.

For a separately approved live run, start from a reviewed allowlist rather than a developer/CI shell. The exact variables needed for official authentication are platform-specific; preserve only those confirmed by official tooling plus basic process/Docker variables. For example, review before use:

```bash
env -i HOME="$HOME" USER="$USER" PATH="$PATH" \
  TMPDIR="${TMPDIR:-/tmp}" DOCKER_HOST="${DOCKER_HOST:-}" \
  uv run --extra strix strix-claude-bridge --help
```

This example intentionally runs help, not inference. Add only officially required authentication/keychain/config variables for an approved probe; do not copy the ambient environment wholesale. A code-level guarantee requires an upstream SDK replace-environment option—`ClaudeAgentOptions.env={}` is only merged over inherited variables and is insufficient.

## Data boundaries

```text
Host CLI/config ---- task/selected tool data ----> trusted Claude SDK ----> Anthropic
     |                                                 |
     | target mount metadata                           | provider session ID
     v                                                 v
Strix Docker/Caido <---- strict MCP tool calls ---- Bridge runtime/state
     |
     +---- intentional findings/reports/SARIF on owner-only host run tree
```

- **Host → provider:** prompts, selected source/request content, mailbox messages, tool results, and findings can be transmitted for inference.
- **Host → Docker:** authorized target mounts/files and commands; no Claude credential or Docker socket mount. Local source mounts can be writable, so shell/`apply_patch` activity can modify the host target.
- **Docker → provider:** only output selected through tool calls, subject to Strix truncation/spill behavior.
- **Local artifacts:** reports intentionally contain security content; bridge transcript/journal/graph are omission/hash metadata by default.
- **Telemetry/retention:** official SDK/provider behavior is not bridge-controlled; review current Anthropic terms.

## Tool authority

Only SDK-created in-process MCP servers are accepted. `tools=[]`, `setting_sources=[]`, `skills=[]`, strict MCP configuration, empty/controlled SDK working directories, and exact `allowed_tools` disable built-ins/project settings/external MCP. Agent ID, parent, sandbox, Caido, coordinator, paths, and report state are closure-bound host authority. Model input is constrained to each copied Strix JSON Schema.

All offensive shell/filesystem actions use bound Strix Docker capabilities. Tool exceptions are sanitized. Timeouts and cancellation preserve indeterminate state instead of claiming rollback. A model cannot undo a side effect already completed.

## Docker boundaries

The standalone `sandbox-probe` uses no network, dropped capabilities, read-only root, no-new-privileges, PID/CPU/memory limits, no mounts, and no inherited environment. The production Strix Docker/Caido bundle is different: it may require networking, writable bind mounts, `NET_ADMIN`, and `NET_RAW` for pentesting. A live fixture scan confirmed that Strix's `apply_patch` tool can modify the bind-mounted host target. Always scan a disposable clone/worktree/copy. The production bundle is not hostile-code or multi-tenant hardening. Docker daemon/image trust and cleanup during daemon failure remain host responsibilities; release image tags are not digest-pinned.

## Logging, journal, and reports

Default durable behavior:

- content-bearing model/tool/user/system/partial events are omitted from transcript payloads;
- journal stores tool/agent/invocation identity, argument/result SHA-256, state, and error flag only;
- session/graph state stores task hashes and graph snapshot stores mailbox content hashes, never raw task/mailbox text;
- no raw command output, request body, credential, tool result, or mailbox content is journaled;
- run tree directories are `0700`, regular files `0600`;
- raw provider session IDs stay in memory for same-process continuation; durable checkpoint state and general events contain no raw ID, only an audit hash in state;
- provider cost is dropped and zero dollars persisted.

`--include-sensitive-content` affects CLI JSONL diagnostics and can expose arbitrary secrets despite best-effort redaction. It does not make the durable journal content-bearing. Explicit vulnerability/dependency/final reports are intentionally content-bearing and must follow target-owner retention policy.

The hash-only tradeoff prevents forensic recovery/replay of tool results and can permit offline confirmation of low-entropy guessed values. It is chosen because restart is disabled and raw content creates a larger confidentiality risk. Hashes support correlation/manual audit only.

Sentinel tests in `tests/test_runtime_state.py` and `tests/test_multi_agent.py` assert argument, output, request-body, and mailbox secrets do not appear in journal/graph files.

## Residual risks

- Active Team login/org and a full root/child fixture scan have one-time live evidence; policy approval, bridge-side org enforcement, arbitrary-target approval, and future behavior remain unverified.
- Provider receives sensitive target data selected during inference.
- SDK subprocess ambient environment is broader than ideal.
- Full-process hard deadlines do not cover every SDK/Docker daemon hang.
- Production Strix Docker is not a hostile/multi-tenant boundary.
- Target/image/dependency supply chain is trusted; image digests are not pinned; writable source mounts permit model-selected host-target changes.
- Full TUI/SQLite integration and restart safety are absent.
- Exact semantic finding dedupe is replaced with deterministic identity to avoid hidden LiteLLM calls.
- Report SARIF can use synthetic locations when upstream finding data lacks a physical source.
- No license or publication approval exists.

## Incident response

Follow [operations](operations.md): interrupt, verify labeled containers, preserve owner-only metadata, manually reconcile every `started` journal entry, and begin a fresh uniquely named run. Never edit hashes/state to force replay and never substitute a provider session ID for a resume token.
