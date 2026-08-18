# Events, sessions, and disabled restart

## Event mapping

| SDK input | Bridge event | Sensitive | Legacy Strix projection |
|---|---|:---:|---|
| `AssistantMessage` / `TextBlock` | `assistant_text {text}` | yes | `raw_response_event` / output-text delta |
| `ToolUseBlock` / server equivalent | `tool_call {call_id,name,arguments}` | yes | `run_item_stream_event: tool_called` |
| string `UserMessage` | `user_message {text}` | yes | generic bridge event only |
| user/assistant `ToolResultBlock` | `tool_result {call_id,content,is_error}` | yes | `run_item_stream_event: tool_output` |
| `StreamEvent` | `partial {message_id,event}` | yes | generic bridge event only |
| `SystemMessage` | `system {subtype,data}` | yes | generic bridge event only |
| `RateLimitEvent` | bounded status/type/reset/utilization/overage metadata | no | generic bridge event |
| `ResultMessage` | `terminal {reason,error,turns,usage,models}` | no | generic bridge event |
| transport/timeout failure | `provider_error {category,...}` | no | generic bridge event |
| unknown SDK frame | `provider_event {message_type}` | no | generic bridge event |
| sandbox/SDK lifecycle | `sandbox_*`, `sdk_cleanup_failed` | no | generic bridge event |

Provider session IDs, rate-event UUIDs, and provider `raw` rate data are deliberately excluded from general events. Provider dollar cost is dropped. `model_usage` mapping keys become bounded model identifiers; when absent, no value is invented. Tests use SDK dataclasses, and live probes/scans observed init/status/stream/assistant/user/tool/rate/result frames; exhaustive variants remain unverified.

## Sinks and redaction

- CLI stdout is JSONL. Sensitive content appears only with explicit `--include-sensitive-content`; this can disclose arbitrary target/provider data.
- `.state/claude-bridge/claude-events.jsonl` is always an owner-only default mirror. Sensitive payloads become `{"omitted": true}`.
- Exact envelope hashes suppress duplicate mirror records.
- Tool and mailbox audit persistence is hash/metadata only. Raw commands, request bodies, credentials, command output, tool results, and mailbox text are not written to the journal/graph snapshot.
- Intentional Strix finding/report artifacts are content-bearing by design and are not “logs”; operators must protect them.

## State files

| File | Authority / contents | Sensitive handling |
|---|---|---|
| `claude-sessions.json` | agent status, task SHA-256, checkpoint metadata/provider-session SHA-256, version/tool/cwd identity | `0600`; no raw task/session ID; restart disabled |
| `claude-events.jsonl` | metadata/omission event mirror | `0600`; never inference authority |
| `claude-usage.json` | auth mode, requests/tokens/cache/model IDs, zero cost | `0600`; no provider spend |
| `tool-journal.json` | invocation/agent/tool, argument/result SHA-256, state, error flag | `0600`; no raw arguments/results; manual audit only |
| `agent-graph.json` | status/parent/name/count/error and mailbox content hashes | `0600`; no mailbox text |
| Strix `run.json`, reports, SARIF | intentional scan/report content | `0600` under `0700` run tree |

## Session and interruption lifecycle

1. One agent runtime owns one active `ClaudeSDKClient`, event pump, and query sequence.
2. A fresh query connects with the configured turn allowance.
3. A continuation disconnects the settled transport and reconnects the same opaque native provider session with only the remaining allowance.
4. Coordinator messages live in memory; snapshot persistence contains hashes only. Mailbox acknowledgment happens only after `continue_with` accepts the query.
5. Timeout/cancellation interrupts while connected, drains when possible, disconnects, records sanitized cleanup errors, then deletes the Strix sandbox.
6. `finish_scan` and `agent_finish` successful `PostToolUse` results are lifecycle barriers.
7. A live root wait interrupted by child completion returned `aborted_tools`; that exact settled same-process result receives a bounded continuation turn rather than being treated as an arbitrary provider failure.

One live reconnect/interruption/hook sequence is verified; exhaustive ordering and failure variants remain unverified.

## Why process-restart resume is disabled

The CLI keeps `--resume-token` only as an explicit rejection guard; any value exits 2 before target setup, run creation, or Docker. Fresh runs emit no token.

Safe restart would require all of the following, none of which is currently complete:

- atomic coordinator graph/status/pending-count/mailbox/notice-claim hydration;
- settled checkpoint for every active agent;
- stable provider tool-use identity exposed to the MCP handler;
- correlation that distinguishes provider replay from a new identical action;
- report/tool dedupe isolation across restart;
- true new-process tests with queued/completed/failed agents and indeterminate effects.

Argument fingerprints cannot establish intent. Therefore completed results are **not** replayed from the journal. A `started` entry is manual reconciliation evidence only.

## Difference from Strix OpenAI Agents / SQLite

Existing Strix uses OpenAI Agents/LiteLLM and SQLite-backed conversation/session behavior. This companion does not patch or replace that path. Claude native session state is inference authority only for the running companion process; JSONL/state files are reporting/diagnostic mirrors. `to_strix_stream_event` covers only text/tool/result compatibility shapes. Full Go TUI/local viewer and SQLite history parity are deferred.
