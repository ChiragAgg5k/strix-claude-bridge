from __future__ import annotations

import io
import json

import pytest

from strix_claude_bridge.events import JsonlEventWriter
from strix_claude_bridge.probe import build_mcp_server, make_sandbox_tool
from strix_claude_bridge.sandbox import ExecResult


class FakeExecutor:
    async def exec(self, command: str) -> ExecResult:
        assert command == "printf ok"
        return ExecResult(exit_code=0, stdout="ok", stderr="")


@pytest.mark.asyncio
async def test_sandbox_tool_result_and_events() -> None:
    output = io.StringIO()
    writer = JsonlEventWriter(output)
    tool = make_sandbox_tool(FakeExecutor(), writer)

    result = await tool.handler({"command": "printf ok"})

    assert result["is_error"] is False
    assert "exit_code=0" in result["content"][0]["text"]
    assert "stdout:\nok" in result["content"][0]["text"]
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event["kind"] for event in events] == ["tool_call", "tool_result"]
    assert events[0]["payload"] == {"tool": "sandbox_exec", "command_bytes": 9}
    assert events[1]["payload"]["stdout_bytes"] == 2
    assert "command" not in events[0]["payload"]
    assert "stdout" not in events[1]["payload"]


@pytest.mark.asyncio
async def test_diagnostic_opt_in_includes_best_effort_redacted_content() -> None:
    output = io.StringIO()
    tool = make_sandbox_tool(
        FakeExecutor(), JsonlEventWriter(output), include_sensitive_content=True
    )

    await tool.handler({"command": "printf ok"})

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert events[0]["payload"]["command"] == "printf ok"
    assert events[1]["payload"]["stdout"] == "ok"


def test_server_registers_exact_mcp_tool_name() -> None:
    server = build_mcp_server(FakeExecutor(), JsonlEventWriter(io.StringIO()))

    assert server["type"] == "sdk"
    assert server["name"] == "strix_spike"
    assert server["instance"].name == "strix_spike"
