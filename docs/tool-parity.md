# Strix tool parity

The authoritative machine-readable inventory is [tool-inventory.json](tool-inventory.json). It contains every effective root/child tool’s exact JSON Schema, schema SHA-256, timeout, role, and generated MCP name for pinned Strix `1.5.3`. Regenerate/check it with:

```bash
uv run python scripts/generate_tool_inventory.py
uv run python scripts/generate_tool_inventory.py --check
```

`tests/test_documentation.py` treats drift as a failure. The inventory uses a quick white-box local-code fixture and no optional skills; a selected skill can change prompt knowledge but the pinned factory inventory shown here is the tested baseline.

## Complete effective inventory

| Strix FunctionTool | Root | Child | Group / parity disposition |
|---|:---:|:---:|---|
| `think` | yes | yes | reasoning helper; original Strix handler |
| `load_skill` | yes | yes | skill loading; original handler, live provider use unverified |
| `create_todo` | yes | yes | todos; original handler with run-isolated storage |
| `list_todos` | yes | yes | todos |
| `update_todo` | yes | yes | todos |
| `mark_todo_done` | yes | yes | todos |
| `mark_todo_pending` | yes | yes | todos |
| `delete_todo` | yes | yes | todos |
| `create_note` | yes | yes | notes; original handler with run-isolated storage |
| `list_notes` | yes | yes | notes |
| `get_note` | yes | yes | notes |
| `update_note` | yes | yes | notes |
| `delete_note` | yes | yes | notes |
| `web_search` | yes | yes | web search; external behavior live-unverified |
| `create_vulnerability_report` | yes | yes | intentional report artifact through Strix report state |
| `create_dependency_report` | yes | yes | intentional report artifact |
| `list_reports` | yes | yes | report state |
| `get_report` | yes | yes | report state |
| `list_requests` | yes | yes | Caido request inventory; production sandbox only |
| `view_request` | yes | yes | Caido request view |
| `repeat_request` | yes | yes | Caido request replay; side-effect journal stores hashes only |
| `list_sitemap` | yes | yes | Caido sitemap |
| `view_sitemap_entry` | yes | yes | Caido sitemap |
| `scope_rules` | yes | yes | Strix scan scope |
| `view_agent_graph` | yes | yes | coordinator graph |
| `send_message_to_agent` | yes | yes | coordinator mailbox; raw message/result never enters journal |
| `wait_for_agents` | yes | yes | coordinator wait; parks inference permit and balances on cancel |
| `create_agent` | yes | yes | bridge callback plus Strix coordinator registration |
| `stop_agent` | yes | yes | coordinator stop/cascade |
| `finish_scan` | yes | no | root lifecycle barrier; successful result stops SDK loop |
| `agent_finish` | no | yes | child lifecycle barrier; exact-one parent notification |
| `view_image` | yes | yes | validated PNG/JPEG/GIF/WebP MCP image block, max 4 MiB |
| `apply_patch` | yes | yes | bound Strix filesystem capability; Docker workspace only |
| `exec_command` | yes | yes | bound Strix shell capability; Docker only; spill/truncation upstream |

Root and child each expose 33 tools; the union is 34 because their lifecycle tool differs.

## MCP naming and schema

For an agent ID sanitized to `root0001`, `exec_command` is `mcp__strix_root0001__exec_command`. Server names are `strix_<sanitized-agent-id>` and `allowed_tools` must exactly equal the SDK-created server inventory. The model cannot select another server or supply agent identity, sandbox, Caido client, coordinator, host path, or report state.

`convert_tool_schema` deep-copies the original `params_json_schema`, requires an object schema and string `required` list, and makes no semantic schema rewrite. Full schemas are intentionally not duplicated in prose; inspect the generated JSON so drift is test-detectable.

## Context and result behavior

1. `ToolContextBinding` closure-binds immutable agent/context authority.
2. A pinned `agents.tool_context.ToolContext` receives only validated JSON arguments plus host-controlled current history.
3. The original Strix `on_invoke_tool` runs; tool business logic is not reimplemented.
4. Strings become MCP text. Existing supported text/image blocks are preserved. Other JSON-serializable structures become sorted JSON text. Arbitrary bytes and invalid/oversized image data are rejected.
5. Raw arguments and normalized results are returned to the live SDK because the model needs them, but durable `tool-journal.json` receives only hashes/state/error status. Intentional Strix reports remain separate explicit artifacts.

## Error, timeout, cancellation, and limits

- Original exceptions are hidden from the model and converted to `<tool> failed (reference <id>)`; secret exception text is not persisted.
- A positive upstream `timeout_seconds` is enforced with `asyncio.wait_for`; timeout output is bounded metadata and the journal stays `started` because side effects are indeterminate.
- Cancellation propagates to the original coroutine and likewise leaves `started` audit state; it is never guessed complete.
- One shared lock-protected counter covers every tool for an agent; default limit is 500.
- `wait_for_agents` temporarily returns the runner semaphore permit while blocked, then reacquires even during cancellation.
- Lifecycle stop occurs only when `finish_scan`/`agent_finish` returns its expected success key.
- Automatic completed-result replay is disabled. Argument/result fingerprints support manual comparison only.

## Known parity gaps

- Autonomous Claude selection, exhaustive browser/Caido/web-search paths, live images, and provider timeout/hook order are unverified.
- Semantic report dedupe differs: this backend uses deterministic exact identity to prevent a hidden LiteLLM request.
- Dynamic enablement and approval callbacks fail closed; `apply_patch` restores its upstream static no-approval contract after capability conversion.
- Optional future Strix tools or schema changes fail the generated inventory/compatibility gate and require review.
