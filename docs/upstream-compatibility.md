# Upstream compatibility and upgrade procedure

## Exact baseline

| Component | Pin / identity | Enforcement |
|---|---|---|
| bridge | `0.1.0` | package/version metadata |
| Strix | `strix-agent==1.5.3` | runtime metadata equality |
| studied Strix source | `8ede419dccf6742aa0e0c4fe3e7faf11c471ff9a` | `verify_strix_source` commit + clean tree |
| OpenAI Agents | `openai-agents[litellm]==0.19.0` | runtime metadata equality |
| Claude Agent SDK | `claude-agent-sdk==0.2.139` | runtime metadata equality |
| Python | package `>=3.10`; Strix extra `>=3.12` | markers/metadata; validation currently Python 3.13 only |

Pins appear in `pyproject.toml`, `uv.lock`, `src/strix_claude_bridge/strix_integration.py`, and the generated tool inventory. Documentation tests reject drift.

## Unchanged upstream APIs and layers

The companion does not patch installed Strix, OpenAI Agents, Claude SDK, or Docker packages. Existing Strix providers, normal `strix` CLI, LiteLLM model configuration, OpenAI `Runner.run_streamed`, SQLite, TUI/viewer, report formats, and Docker image remain upstream behavior. There is no native backend selector.

## Adapted/public-ish seams

| Seam | Symbol/file | Use |
|---|---|---|
| target setup | `strix.interface.scan_setup.build_targets_info` | canonical target normalization |
| repository/spec staging | `strix.interface.utils.*` | same local source/workspace inputs |
| prompt/tool creation | `strix.agents.factory.build_strix_agent` | rendered instructions and original FunctionTools |
| scope/task | `strix.core.inputs.{build_scope_context,build_root_task}` | canonical context/task |
| sandbox creation | `strix.runtime.session_manager.create_or_reuse` | real Strix Docker/Caido bundle |
| coordinator | `strix.core.agents.AgentCoordinator` | graph/status/mailbox runtime |
| terminal notice | `strix.core.execution.notify_parent_on_terminal` | exact-one parent outcome |
| graph tools | `strix.tools.agents_graph.*` | create/send/wait/stop/finish contracts |
| report state | `strix.report.state.ReportState` | findings, usage, writers, final status |
| tool context | `agents.tool_context.ToolContext` | invokes original handlers |
| capabilities | `agents.sandbox.capabilities.{Filesystem,Shell}` | bound capability clones |
| SDK client/options | `claude_agent_sdk.{ClaudeSDKClient,ClaudeAgentOptions}` | provider loop/session transport |
| MCP server/tool | `create_sdk_mcp_server`, `tool` | strict in-process bridge |

## Pinned/private breakpoints

- `session_manager._SESSION_CACHE` is used to remove the owned cache entry before verified deletion.
- Note/todo module storage/path globals are isolated and restored per run.
- Capability clones must expose FunctionTools and `apply_patch` approval representation is normalized to its original static policy.
- Coordinator internal lock/runtime mailbox/pending fields are used for delivery acknowledgment and metadata snapshot.
- Report `_llm_usage.zero_cost` and exact dedupe replacement are companion-specific compatibility seams.
- SDK option fields (`strict_mcp_config`, `setting_sources`, `skills`, `hooks`, `resume`) and bundled subprocess behavior are version-sensitive.
- Strix standalone PyInstaller builds do not automatically collect/sign the SDK bundled executable.

Every seam above is a likely upgrade breakpoint and justifies exact fail-closed pins.

## Upgrade procedure

1. Create a clean study checkout of the candidate Strix revision; do not patch it.
2. Review changelogs/terms for Strix, OpenAI Agents, Claude SDK, MCP, Docker, and Anthropic authentication/policy.
3. Update constants and dependency pins together; run `uv lock` deliberately.
4. Diff all symbols in the tables above, including private fields and report/tool schemas.
5. Run `uv run python scripts/generate_tool_inventory.py`; review every tool/schema/timeout/role change.
6. Run documentation checks; update ownership, status, parity, events, security, ADR consequences, and compatibility categories.
7. Run lock/sync, Ruff, all deterministic tests, three Docker tests, stable single/multi-agent fixture scans, report/SARIF/permission/redaction inspection, build/metadata/help, cleanup, and staging checks.
8. On each supported release platform, collect/permission/sign/notarize and smoke the SDK executable.
9. Only after separate policy/org/target-data approvals, run harmless live auth/MCP/event/cancel evidence; then a narrowly authorized scan.
10. Obtain independent correctness/security acceptance before changing compatibility claims.

## Upstream PR candidates

- Native backend dispatch (`ExecutionBackend`) in Strix without treating agent-loop backends as LiteLLM models.
- Backend-neutral sandbox FunctionTools and explicit capability export.
- Public coordinator interrupt/mailbox acknowledgment and sanitized snapshot hooks.
- Normalized event/transcript sink interfaces for TUI/viewer/SQLite.
- Provider checkpoint storage and a stable tool-use identity/reconciliation contract.
- Subscription usage fields separate from API dollar budget.
- Public sandbox deletion verification and report deterministic-dedupe injection.
- PyInstaller collection hooks for Claude Agent SDK’s platform executable.

Until those land and are reviewed, this remains a version-pinned companion.
