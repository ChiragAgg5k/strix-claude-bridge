# Operations and incident guide

## Safe preflight

1. Confirm every target is owned or explicitly authorized.
2. Re-check current Anthropic subscription/third-party policy and approved organization through an official interface. Do not inspect credential files.
3. Ensure API/cloud overrides are unset and Docker is healthy.
4. Create a disposable clone/worktree/copy: live evidence confirms Strix shell/`apply_patch` tools can modify a bind-mounted host target.
5. Use a unique `--run-name`; store JSONL with owner-only permissions because diagnostics can contain target metadata.
6. Start with `--max-concurrent-agents 2` and a finite `--max-runtime`.

```bash
umask 077
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL \
  ANTHROPIC_CUSTOM_HEADERS AWS_BEARER_TOKEN_BEDROCK \
  CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY
cp -R ./authorized-app /tmp/authorized-app-scan
uv run --extra strix strix-claude-bridge scan \
  --experimental --authorized-use \
  --target /tmp/authorized-app-scan --run-name authorized-run \
  --scan-mode quick --max-turns 100 --max-runtime 3600 \
  --max-concurrent-agents 2 --max-agents 8 \
  > authorized-run.bridge.jsonl
```

Do not run live subscription execution in shared CI or as a hosted service.

## Stop and interruption

Ctrl-C requests cancellation, settles/disconnects each SDK client, persists an interrupted run when possible, and then deletes the Strix sandbox. Tool side effects completed before interruption are not rolled back.

After an abnormal exit:

```bash
docker ps -a --filter label=org.strix.runtime
find strix_runs/authorized-run/.state/claude-bridge -maxdepth 1 -type f -ls
```

Do not delete a suspected leaked container without confirming it belongs to this authorized run. A Docker daemon failure can prevent verified cleanup.

## Process restart

Process-restart resume is disabled. The CLI rejects `--resume-token` before target preparation and Docker startup because the bridge cannot safely restore the complete coordinator graph or distinguish provider replay from a newly intended identical action. After interruption, inspect `tool-journal.json`, reconcile any `started` side effect manually, and use a fresh unique run name. Never pass an opaque SDK session ID to the CLI.

A rate/plan-limit error can retain diagnostic checkpoint metadata, but that metadata is not an authorization to resume. Wait for capacity reset, lower concurrency, and start a fresh run. No dollar amount represents subscription quota.

## Artifacts

Public scan artifacts live directly under `strix_runs/<run>/`. Sensitive bridge state lives under `.state/claude-bridge/` and should remain mode `0700`/`0600`:

- `claude-sessions.json`: agent status plus task/provider-session SHA-256 audit hashes and checkpoint metadata; no raw task or provider session ID (restart disabled);
- `tool-journal.json`: agent/tool/invocation metadata, argument/result hashes, state, and error status—never raw arguments/results;
- `claude-events.jsonl`: metadata transcript mirror;
- `claude-usage.json`: zero-dollar subscription token/model accounting;
- `agent-graph.json`: coordinator status/counts and mailbox content hashes—never mailbox text.

Do not upload `.state` or diagnostic sensitive JSONL to issue trackers. Report output may itself contain target secrets and must follow the target owner's retention policy. The bridge normalizes the run tree to owner-only directories (`0700`) and files (`0600`), rather than relying only on caller umask.

A live scan sends task prompts, selected source/request data, mailbox messages, tool results, and findings through the official SDK to Anthropic. Obtain target-owner approval for this transfer and review current provider retention, privacy, and SDK telemetry terms. The bridge controls local artifacts, not provider-side handling.

## Common errors

- `authentication_error`: remove API/cloud override variables; this still does not attest subscription eligibility.
- `rate or plan limit reached`: lower concurrency and start a fresh run after capacity resets.
- `indeterminate tool invocation`: manual side-effect reconciliation required; restart resume is disabled.
- `--resume-token is disabled`: reconcile side effects and start a fresh uniquely named run.
- `sandbox_cleanup_failed`: inspect Docker immediately; a successful scan returns nonzero when deletion cannot be verified.
- `maximum agent count is exhausted`: stop unnecessary children or raise the host-approved bound.

## Release checklist

Before publication: current policy/written approval, a bridge-enforceable organization attestation procedure, safe writable-target handling, a safe restart design, license selection, unified Strix integration decision, post-live independent acceptance, and five-platform bundled-executable build/sign/smoke results are still required. Team auth/MCP/concurrency/cancellation/tool-timeout and one full root/child fixture scan are recorded, but they do not imply the remaining approvals or release readiness.
