from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from strix_claude_bridge import strix_integration
from strix_claude_bridge.backend import SessionCheckpoint, SessionCompatibilityError
from strix_claude_bridge.strix_integration import (
    StrixAgentBridgeInput,
    build_claude_session_spec,
)


class FunctionTool:
    name = "thinking"
    description = "Record a thought"
    params_json_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"thought": {"type": "string"}},
        "required": ["thought"],
        "additionalProperties": False,
    }
    timeout_seconds = 1

    async def on_invoke_tool(self, context: Any, raw: str) -> str:
        assert context.context["sandbox_session"] == "bound-session"
        assert "thought" in raw
        return "ok"


def test_strix_seam_builds_per_agent_server_and_exact_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compatibility_checked = False

    def compatible() -> None:
        nonlocal compatibility_checked
        compatibility_checked = True

    monkeypatch.setattr(strix_integration, "verify_runtime_compatibility", compatible)
    spec = build_claude_session_spec(
        StrixAgentBridgeInput(
            agent_id="root.agent",
            system_prompt="Rendered Strix prompt",
            cwd=tmp_path,
            context={"agent_id": "root.agent", "sandbox_session": "bound-session"},
            function_tools=[FunctionTool()],
            max_turns=5,
        )
    )

    assert compatibility_checked
    assert spec.agent_id == "root.agent"
    assert spec.allowed_tools == ("mcp__strix_root_agent__thinking",)
    assert tuple(spec.mcp_servers) == ("strix_root_agent",)
    server = spec.mcp_servers["strix_root_agent"]
    assert server.configuration["type"] == "sdk"
    assert server.configuration["instance"].name == "strix_root_agent"


def test_root_and_child_lifecycle_tools_are_terminal_barriers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(strix_integration, "verify_runtime_compatibility", lambda: None)

    root_tool = FunctionTool()
    root_tool.name = "finish_scan"
    child_tool = FunctionTool()
    child_tool.name = "agent_finish"
    spec = build_claude_session_spec(
        StrixAgentBridgeInput(
            agent_id="child",
            system_prompt="prompt",
            cwd=tmp_path,
            context={"agent_id": "child", "sandbox_session": "bound-session"},
            function_tools=[root_tool, child_tool],
        )
    )

    assert spec.terminal_tools == (
        "mcp__strix_child__finish_scan",
        "mcp__strix_child__agent_finish",
    )


def test_strix_seam_rejects_provider_checkpoint_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(strix_integration, "verify_runtime_compatibility", lambda: None)
    checkpoint = SessionCheckpoint(
        provider_backend="claude-agent-sdk",
        backend_version="0.1.0",
        sdk_version=strix_integration.SUPPORTED_CLAUDE_SDK_VERSION,
        model=None,
        tool_schema_digest="digest",
        provider_session_id="opaque",
        cwd_identity="cwd",
        last_settled_turn=1,
    )
    with pytest.raises(SessionCompatibilityError, match="checkpoint resume is disabled"):
        build_claude_session_spec(
            StrixAgentBridgeInput(
                agent_id="root",
                system_prompt="prompt",
                cwd=tmp_path,
                context={"agent_id": "root"},
                function_tools=[FunctionTool()],
                resume_checkpoint=checkpoint,
            )
        )


@pytest.mark.asyncio
async def test_real_strix_think_crosses_sdk_mcp_control_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thinking_module = pytest.importorskip("strix.tools.thinking.tool")
    think = thinking_module.think

    monkeypatch.setattr(strix_integration, "verify_runtime_compatibility", lambda: None)
    spec = build_claude_session_spec(
        StrixAgentBridgeInput(
            agent_id="root",
            system_prompt="Rendered Strix prompt",
            cwd=tmp_path,
            context={"agent_id": "root"},
            function_tools=[think],
        )
    )
    instance = spec.mcp_servers["strix_root"].configuration["instance"]
    request = CallToolRequest(
        params=CallToolRequestParams(name="think", arguments={"thought": "authorized fixture only"})
    )

    response = await instance.request_handlers[CallToolRequest](request)

    assert response.root.isError is False
    assert '"success": true' in response.root.content[0].text
