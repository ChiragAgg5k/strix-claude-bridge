# Strix Claude Bridge — Implementation Plan

## Goal

Build and publish an open-source Strix execution backend that uses the official Claude Agent SDK with a user's own eligible Claude subscription, while preserving Strix's sandbox, pentesting tools, multi-agent coordination, findings, and reports.

Each user must authenticate locally through the Agent SDK's supported flow. The bridge must not extract Claude Code credentials, replay OAuth tokens against private endpoints, provide a hosted Claude login, pool subscription capacity, or proxy one user's subscription for another.

## Current Strix Architecture

Strix currently uses:

- `openai-agents` for its agent loop
- LiteLLM for Anthropic and other model providers
- `Runner.run_streamed` for model/tool execution
- `SandboxAgent` for shell and filesystem capabilities
- A Docker sandbox containing the pentesting toolchain
- A custom coordinator for root and child agents
- SQLite-backed conversation sessions
- Hooks for token, cost, turn, and budget accounting
- Structured event streams for the TUI and local viewer

The existing Anthropic provider requires an Anthropic API key. Claude subscription authentication is not implemented. The existing ChatGPT subscription integration is provider-specific and should not be copied for Claude.

## Recommended Architecture

Implement a separate Claude Agent SDK execution backend instead of pretending the Agent SDK is a raw LiteLLM model provider.

Each Strix agent should have:

- One `ClaudeSDKClient` session
- The rendered Strix system prompt
- An in-process MCP server exposing its permitted Strix tools
- Its own coordinator identity and context
- A translation layer from Agent SDK events to Strix events
- Explicit lifecycle, cancellation, and resume handling

The existing Docker sandbox remains responsible for shell, browser, proxy, scanning, and workspace isolation.

## Phase 1: Research and Compatibility Spike

1. Pin the Strix revision used as the integration base.
2. Install and test the Python `claude-agent-sdk` package.
3. Authenticate Claude Code with the intended Team organization.
4. Confirm `/status` reports subscription authentication for the approved organization.
5. Ensure `ANTHROPIC_API_KEY` is unset so it does not override subscription authentication.
6. Run a minimal Agent SDK query using the existing subscription login.
7. Test a custom in-process MCP tool.
8. Record subscription limit, concurrency, model selection, and headless behavior.
9. Verify cancellation and timeout behavior.

### Exit criteria

- A Python process can run an Agent SDK session through the Team subscription.
- Claude can invoke a custom MCP tool and receive its result.
- Streaming events, session identifiers, usage data, and errors are understood.

## Phase 2: Minimal Single-Agent Prototype

1. Create a small adapter outside Strix first.
2. Load a rendered Strix system prompt.
3. Expose a minimal tool set:
   - thinking
   - shell execution
   - file reading
   - file writing or patching
   - finish
4. Route shell and filesystem operations through the Strix Docker sandbox.
5. Run a single authorized local-code security task.
6. Capture assistant messages, tool calls, tool results, and final output.

### Exit criteria

- One Claude Agent SDK agent can inspect a mounted test repository.
- All command execution occurs inside the Strix sandbox.
- The agent can complete without using the existing `Runner.run_streamed` loop.

## Phase 3: Strix Tool Bridge

Expose Strix tools through an in-process MCP server while preserving their schemas and behavior.

Tool groups:

- Sandbox shell and filesystem
- Browser automation
- Caido proxy operations
- Web search
- Notes and todos
- Skill loading
- Vulnerability and dependency reporting
- Image viewing
- Agent communication
- Scan completion

Tasks:

1. Create an adapter from Strix/OpenAI function-tool schemas to Agent SDK MCP schemas.
2. Build a context object containing the sandbox session, agent ID, coordinator, and output store.
3. Normalize tool results and errors.
4. Preserve output truncation and workspace spill behavior.
5. Preserve tool timeouts and cancellation.
6. Add tests for structured arguments, large outputs, failures, and binary/image responses.

### Exit criteria

- The root agent can access the same effective tools as the current Strix root agent.
- Existing tool implementations are reused where practical rather than duplicated.

## Phase 4: Execution Backend

Introduce an explicit execution abstraction, for example:

```text
ExecutionBackend
├── OpenAIAgentsBackend
└── ClaudeAgentSDKBackend
```

The Claude backend must support:

- Starting a session
- Streaming events
- Continuing a session
- Injecting coordinator messages
- Interrupting a running turn
- Cancelling an agent
- Detecting terminal output
- Mapping provider errors
- Returning usage information

Avoid implementing the integration as a LiteLLM model identifier. The Agent SDK owns an agent loop, so nesting it inside the existing OpenAI Agents loop would produce conflicting tool and conversation state.

### Exit criteria

- A configuration option can select the existing backend or Claude Agent SDK backend.
- Existing providers continue to work unchanged.
- A complete single-agent Strix scan can run through the Claude backend.

## Phase 5: Multi-Agent Coordination

1. Create one Agent SDK session per Strix root or child agent.
2. Reuse the existing agent graph and coordinator where possible.
3. Port `create_agent`, `send_message_to_agent`, `wait_for_agents`, `stop_agent`, and `agent_finish` behavior.
4. Support parent-to-child context inheritance without duplicating excessive history.
5. Preserve completion notifications when an agent fails or is cancelled.
6. Add concurrency controls to avoid exhausting Team subscription limits.
7. Confirm that interrupted agents can resume with queued messages.

### Exit criteria

- Root and child agents can run concurrently.
- Child findings reach the parent reliably.
- Failed children cannot leave the parent waiting indefinitely.

## Phase 6: Events, Sessions, and Resume

1. Translate Agent SDK messages into Strix transcript events.
2. Preserve compatibility with the TUI and local viewer.
3. Store the Agent SDK session ID alongside Strix run metadata.
4. Implement resume for interrupted scans.
5. Decide whether Strix SQLite history remains authoritative or becomes a reporting mirror.
6. Prevent duplicate tool execution after resume.
7. Test malformed, expired, and incompatible resume tokens.

### Exit criteria

- Live scans render correctly in existing Strix interfaces.
- A stopped scan can resume without losing findings or repeating destructive actions.

## Phase 7: Usage and Limits

Subscription usage is not equivalent to API dollar billing.

1. Track requests, input tokens, output tokens, and model names when available.
2. Record authentication mode as `claude_subscription`.
3. Do not report subscription usage as API spend.
4. Replace or disable `--max-budget` dollar enforcement for subscription runs.
5. Preserve `--max-turns` and add concurrency/runtime limits.
6. Surface rate-limit and plan-limit errors clearly.
7. Stop cleanly when subscription capacity is exhausted.

Potential controls:

- `--max-turns`
- `--max-runtime`
- `--max-concurrent-agents`
- `--max-tool-calls-per-turn`

## Phase 8: Security and Policy Controls

1. Require explicit confirmation that targets are owned or authorized.
2. Keep all offensive commands inside the Docker sandbox.
3. Do not export Claude OAuth credentials into the sandbox.
4. Do not log credentials, target secrets, or full sensitive model traffic.
5. Confirm the Claude login belongs to the cyber-approved Anthropic organization.
6. Publish this as a user-run integration, not a hosted service that provides access to Claude subscriptions.
7. Require every user to authenticate directly with Anthropic using the official Agent SDK flow.
8. Do not implement a hosted Claude login flow for third-party users.
9. Document data handling, telemetry behavior, supported plans, and Anthropic policy constraints.

## Phase 9: Testing

### Unit tests

- Tool schema conversion
- Tool result conversion
- Event translation
- Usage accounting
- Authentication-mode detection
- Cancellation and timeout mapping
- Coordinator message injection
- Resume metadata

### Integration tests

- Single-agent local repository scan
- Root and child agent scan
- Tool failure recovery
- Context-window pressure
- Rate-limit handling
- User cancellation
- Resume after process restart
- Report and SARIF generation

### Security regression targets

Use intentionally vulnerable local applications only, such as:

- OWASP Juice Shop
- DVWA
- WebGoat
- Small purpose-built vulnerable fixtures

Never run automated tests against third-party systems.

## Proposed Configuration

Exact names can change during implementation, but the interface should be explicit:

```bash
export STRIX_AGENT_BACKEND="claude-agent-sdk"
export STRIX_LLM="claude-sonnet"

strix -n \
  --target ./authorized-test-app \
  --scan-mode quick \
  --max-turns 100 \
  --max-concurrent-agents 2
```

Startup validation should fail with a helpful message when:

- Claude Code/Agent SDK is not authenticated
- The active credential is an API key when subscription use was requested
- The selected organization does not match configured policy
- Docker is unavailable

## Initial Deliverables

1. Architecture decision record
2. Minimal Agent SDK authentication and MCP spike
3. Single-agent Strix prototype
4. Tool bridge with tests
5. Multi-agent backend
6. Event and resume compatibility
7. Usage-limit controls
8. Security and deployment documentation

## Non-Goals for the First Version

- Public SaaS access using customer Claude subscriptions
- Custom Claude OAuth implementation
- Token extraction or private endpoint emulation
- Full parity with every Strix provider
- CI deployment using shared personal credentials
- Removing the existing OpenAI Agents/LiteLLM backend

## Key Risks

- Agent SDK and Strix both own agent-loop responsibilities.
- Team subscription limits may be too restrictive for deep multi-agent scans.
- Agent SDK event semantics may not map cleanly to existing Strix events.
- Session resume may differ from Strix's SQLite model.
- Some Strix tools depend directly on OpenAI Agents SDK context types.
- Anthropic authentication and subscription policies can change.
- Cyber safeguards apply only when the correct approved organization is active.

## First Implementation Task

Create an isolated Python spike that:

1. Uses the official Claude Agent SDK.
2. Authenticates through the existing Team subscription login.
3. Registers one in-process MCP tool named `sandbox_exec`.
4. Executes that tool against a disposable Docker container.
5. Streams all events to JSON Lines.
6. Supports Ctrl-C cancellation.
7. Writes a short compatibility report covering authentication, events, usage, limits, and errors.
