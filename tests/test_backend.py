from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from strix_claude_bridge.auth import AuthenticationModeError
from strix_claude_bridge.backend import (
    AgentSessionSpec,
    BackendStateError,
    InProcessMCPServer,
    SessionCheckpoint,
    SessionCompatibilityError,
)
from strix_claude_bridge.claude_backend import ClaudeAgentSDKBackend, SessionState


def result(session_id: str, *, reason: str = "completed", turns: int = 1) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=2,
        duration_api_ms=1,
        is_error=reason != "completed",
        num_turns=turns,
        session_id=session_id,
        usage={"input_tokens": 1, "output_tokens": 2},
        terminal_reason=reason,
    )


def server() -> InProcessMCPServer:
    @tool("thinking", "Think", {"type": "object", "properties": {}})
    async def thinking(_arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}]}

    configuration = create_sdk_mcp_server("strix_root", tools=[thinking])
    allowed = ("mcp__strix_root__thinking",)
    return InProcessMCPServer("strix_root", configuration, allowed)


class ReusableClient:
    instance: ReusableClient | None = None

    def __init__(self, options: Any) -> None:
        self.options = options
        self.connected = False
        self.disconnected = False
        self.queries: list[str] = []
        self.__class__.instance = self

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        index = len(self.queries)
        yield AssistantMessage(content=[TextBlock(f"turn-{index}")], model="claude")
        yield result(f"provider-session-{index}")

    async def interrupt(self) -> None:
        raise AssertionError("settled turns should not be interrupted")


class HangingClient(ReusableClient):
    instance: HangingClient | None = None

    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.interrupted = asyncio.Event()
        self.interrupted_while_connected = False
        self.__class__.instance = self

    async def receive_response(self):
        await asyncio.Event().wait()
        if False:
            yield None

    async def interrupt(self) -> None:
        self.interrupted_while_connected = self.connected
        self.interrupted.set()


class TimeoutDrainClient(ReusableClient):
    instance: TimeoutDrainClient | None = None

    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.interrupted = asyncio.Event()
        self.__class__.instance = self

    async def receive_response(self):
        if self.options.resume is None:
            await self.interrupted.wait()
            yield result("provider-session", reason="aborted_streaming")
        else:
            yield AssistantMessage(content=[TextBlock("second")], model="claude")
            yield result("provider-session")

    async def interrupt(self) -> None:
        self.interrupted.set()


class LifecycleClient(ReusableClient):
    async def receive_response(self):
        matcher = self.options.hooks["PostToolUse"][0]
        callback = matcher.hooks[0]
        hook_result = await callback(
            {
                "tool_name": "mcp__strix_root__thinking",
                "tool_response": '{"success":true,"scan_completed":true}',
            },
            "call-1",
            {"signal": None},
        )
        assert hook_result["continue_"] is False
        yield result("provider-session", reason="aborted_streaming")


class FailingCleanupClient(HangingClient):
    instance: FailingCleanupClient | None = None

    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.calls: list[str] = []
        self.__class__.instance = self

    async def interrupt(self) -> None:
        self.calls.append("interrupt")
        raise RuntimeError("interrupt failed")

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.disconnected = True
        raise RuntimeError("disconnect failed")


def spec(
    tmp_path: Path,
    *,
    timeout: float | None = None,
    max_turns: int = 4,
    terminal: bool = False,
) -> AgentSessionSpec:
    mcp_server = server()
    return AgentSessionSpec(
        agent_id="root",
        system_prompt="Authorized local test.",
        cwd=tmp_path,
        mcp_servers={"strix_root": mcp_server},
        allowed_tools=mcp_server.allowed_tools,
        model="claude-sonnet",
        max_turns=max_turns,
        turn_timeout_s=timeout,
        terminal_tools=mcp_server.allowed_tools if terminal else (),
    )


@pytest.mark.asyncio
async def test_session_reconnects_native_session_with_remaining_turn_budget(
    tmp_path: Path,
) -> None:
    session = ClaudeAgentSDKBackend(client_factory=ReusableClient).create_session(spec(tmp_path))

    await session.start("first")
    first = [event async for event in session.events()]
    await session.inject("coordinator message")
    second = [event async for event in session.events()]
    await session.close()

    client = ReusableClient.instance
    assert client is not None
    assert client.queries == ["coordinator message"]
    assert client.disconnected
    assert client.options.resume == "provider-session-1"
    assert client.options.max_turns == 3
    assert [event.kind for event in first] == ["assistant_text", "terminal"]
    assert [event.sequence for event in [*first, *second]] == [1, 2, 3, 4]
    assert first[0].sensitive is True
    assert session.result is not None
    assert session.result.provider_session_id == "provider-session-1"
    assert session.state is SessionState.CLOSED
    assert client.options.tools == []
    assert client.options.strict_mcp_config is True
    assert client.options.setting_sources == []
    assert client.options.skills == []
    assert client.options.mcp_servers["strix_root"]["type"] == "sdk"


@pytest.mark.asyncio
async def test_continue_requires_prior_event_consumption(tmp_path: Path) -> None:
    session = ClaudeAgentSDKBackend(client_factory=ReusableClient).create_session(spec(tmp_path))
    await session.start("first")
    await asyncio.sleep(0)

    with pytest.raises(BackendStateError, match="events must be consumed"):
        await session.continue_with("too early")

    _ = [event async for event in session.events()]
    await session.close()


@pytest.mark.asyncio
async def test_turn_timeout_drains_aborted_result_before_continuation(tmp_path: Path) -> None:
    session = ClaudeAgentSDKBackend(
        client_factory=TimeoutDrainClient, interrupt_grace_s=0.1
    ).create_session(spec(tmp_path, timeout=0.01))

    await session.start("first")
    first = [event async for event in session.events()]
    assert [event.kind for event in first] == ["terminal", "provider_error"]
    assert first[-1].payload == {"category": "timeout", "timeout_seconds": 0.01}
    assert session.result is not None
    assert session.result.terminal_reason == "timeout"

    await session.continue_with("second")
    second = [event async for event in session.events()]
    await session.close()

    assert [event.kind for event in second] == ["assistant_text", "terminal"]
    assert session.result is not None
    assert session.result.terminal_reason == "completed"
    client = TimeoutDrainClient.instance
    assert client is not None
    assert client.queries == ["second"]
    assert client.options.resume == "provider-session"
    assert client.options.max_turns == 3


@pytest.mark.asyncio
async def test_undrained_timeout_is_not_reusable(tmp_path: Path) -> None:
    session = ClaudeAgentSDKBackend(
        client_factory=HangingClient, interrupt_grace_s=0.01
    ).create_session(spec(tmp_path, timeout=0.01))

    await session.start("first")
    events = [event async for event in session.events()]

    assert events[-1].payload["category"] == "timeout"
    assert session.state is SessionState.FAILED
    with pytest.raises(BackendStateError, match="prior turn"):
        await session.continue_with("unsafe")
    await session.close()


@pytest.mark.asyncio
async def test_post_tool_hook_classifies_successful_lifecycle_stop(tmp_path: Path) -> None:
    session = ClaudeAgentSDKBackend(client_factory=LifecycleClient).create_session(
        spec(tmp_path, terminal=True)
    )

    await session.start("first")
    events = [event async for event in session.events()]
    await session.close()

    assert events[-1].kind == "terminal"
    assert events[-1].payload["lifecycle_tool"] is True
    assert events[-1].payload["is_error"] is False
    assert session.result is not None
    assert session.result.terminal_reason == "completed"
    assert session.result.is_error is False


@pytest.mark.asyncio
async def test_cancel_interrupts_before_disconnect(tmp_path: Path) -> None:
    session = ClaudeAgentSDKBackend(
        client_factory=HangingClient, interrupt_grace_s=0.1
    ).create_session(spec(tmp_path))
    await session.start("first")

    await session.cancel()

    client = HangingClient.instance
    assert client is not None
    assert client.interrupted_while_connected
    assert client.disconnected
    assert session.state is SessionState.CLOSED
    assert session.result is not None
    assert session.result.terminal_reason == "cancelled"


@pytest.mark.asyncio
async def test_cleanup_disconnects_even_when_interrupt_fails(tmp_path: Path) -> None:
    session = ClaudeAgentSDKBackend(
        client_factory=FailingCleanupClient, interrupt_grace_s=0.01
    ).create_session(spec(tmp_path))
    await session.start("first")

    with pytest.raises(RuntimeError, match="interrupt failed"):
        await session.close()

    client = FailingCleanupClient.instance
    assert client is not None
    assert client.calls == ["interrupt", "disconnect"]
    assert client.disconnected
    assert session.state is SessionState.CLOSED


@pytest.mark.asyncio
async def test_subscription_override_refuses_before_client_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed = False

    def factory(_options: Any) -> Any:
        nonlocal constructed
        constructed = True
        return ReusableClient(_options)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "hidden")
    session = ClaudeAgentSDKBackend(client_factory=factory).create_session(spec(tmp_path))

    with pytest.raises(AuthenticationModeError, match="ANTHROPIC_API_KEY"):
        await session.start("first")
    assert constructed is False


def test_resume_and_external_mcp_authority_fail_closed(tmp_path: Path) -> None:
    mcp_server = server()
    with pytest.raises(SessionCompatibilityError, match="resume is disabled"):
        AgentSessionSpec(
            agent_id="root",
            system_prompt="prompt",
            cwd=tmp_path,
            mcp_servers={"strix_root": mcp_server},
            allowed_tools=mcp_server.allowed_tools,
            resume_session_id="unchecked-authority",
        )
    with pytest.raises(ValueError, match="in-process"):
        InProcessMCPServer(
            "external",
            {"type": "stdio", "name": "external", "command": "sh"},
            ("mcp__external__x",),
        )


def test_structured_settled_checkpoint_resume_is_disabled(tmp_path: Path) -> None:
    mcp_server = server()
    checkpoint = SessionCheckpoint(
        provider_backend="claude-agent-sdk",
        backend_version="0.1.0",
        sdk_version="0.2.139",
        model="claude-sonnet",
        tool_schema_digest="digest",
        provider_session_id="opaque-provider-session",
        cwd_identity="cwd",
        last_settled_turn=1,
    )
    with pytest.raises(SessionCompatibilityError, match="checkpoint resume is disabled"):
        AgentSessionSpec(
            agent_id="root",
            system_prompt="prompt",
            cwd=tmp_path,
            mcp_servers={"strix_root": mcp_server},
            allowed_tools=mcp_server.allowed_tools,
            resume_checkpoint=checkpoint,
        )


@pytest.mark.asyncio
async def test_continuation_gets_only_remaining_turn_allowance(tmp_path: Path) -> None:
    options_seen: list[Any] = []

    class EightThenThreeClient(ReusableClient):
        def __init__(self, options: Any) -> None:
            super().__init__(options)
            options_seen.append(options)

        async def receive_response(self):
            turns = 8 if self.options.resume is None else 3
            yield result("budget-session", turns=turns)

    session = ClaudeAgentSDKBackend(client_factory=EightThenThreeClient).create_session(
        spec(tmp_path, max_turns=10)
    )
    await session.start("first")
    _ = [event async for event in session.events()]
    await session.continue_with("second")
    _ = [event async for event in session.events()]

    assert [options.max_turns for options in options_seen] == [10, 2]
    assert options_seen[1].resume == "budget-session"
    assert session.result is not None
    assert session.result.terminal_reason == "turn_limit_exceeded"
    assert session.result.is_error is True
    await session.close()


@pytest.mark.asyncio
async def test_cumulative_turn_limit_blocks_another_query(tmp_path: Path) -> None:
    session = ClaudeAgentSDKBackend(client_factory=ReusableClient).create_session(
        spec(tmp_path, max_turns=1)
    )
    await session.start("first")
    _ = [event async for event in session.events()]

    with pytest.raises(BackendStateError, match="turn limit"):
        await session.continue_with("second")
    await session.close()
