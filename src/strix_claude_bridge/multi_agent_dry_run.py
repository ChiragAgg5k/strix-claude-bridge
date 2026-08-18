"""Credential-free scripted root/child clients over the real Strix MCP seams."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from mcp.types import CallToolRequest, CallToolRequestParams

from strix_claude_bridge.dry_run import _FIXTURE_MARKER, _response_text


def build_multi_agent_dry_run_client_factory(*, workspace_subdir: str) -> Callable[[Any], Any]:
    if not workspace_subdir or "/" in workspace_subdir or ".." in workspace_subdir:
        raise ValueError("dry-run workspace subdirectory is invalid")

    class MultiAgentDryRunClient:
        active = 0
        peak_active = 0
        sessions: ClassVar[list[str]] = []

        def __init__(self, options: Any) -> None:
            self.options = options
            server = next(iter(options.mcp_servers.values()))
            self.instance = server["instance"]
            self.server_name = server["name"]
            self.allowed = set(options.allowed_tools)
            self.connected = False
            self.query_count = 0
            self.is_root = any(item.endswith("__finish_scan") for item in self.allowed)
            self.session_id = (
                f"simulated-{'root' if self.is_root else 'child'}-{len(self.sessions) + 1}"
            )
            self.sessions.append(self.session_id)

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def interrupt(self) -> None:
            return

        async def query(self, _prompt: str) -> None:
            self.query_count += 1

        async def _call(self, name: str, arguments: dict[str, Any]) -> str:
            request = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments))
            response = await self.instance.request_handlers[CallToolRequest](request)
            text = _response_text(response)
            if bool(getattr(response.root, "isError", False)):
                raise RuntimeError(f"scripted tool {name} failed")
            return text

        async def _tool(self, name: str, arguments: dict[str, Any], call_id: str):
            yield AssistantMessage(
                content=[ToolUseBlock(call_id, f"mcp__{self.server_name}__{name}", arguments)],
                model="fake-agent-sdk",
            )
            result = await self._call(name, arguments)
            yield UserMessage(content=[ToolResultBlock(call_id, result, False)])

        async def receive_response(self):
            type(self).active += 1
            type(self).peak_active = max(type(self).peak_active, type(self).active)
            try:
                if self.is_root:
                    create = {
                        "name": "Fixture Specialist",
                        "task": "Inspect the authorized vulnerable fixture and report one finding.",
                        "inherit_context": False,
                        "skills": [],
                    }
                    async for item in self._tool("create_agent", create, "root-create"):
                        yield item
                    async for item in self._tool(
                        "wait_for_agents",
                        {"reason": "Wait for fixture specialist", "timeout_seconds": 30},
                        "root-wait",
                    ):
                        yield item
                    finish = {
                        "executive_summary": "One path traversal was confirmed by a child agent.",
                        "methodology": (
                            "Delegated fixture inspection through the Strix coordinator."
                        ),
                        "technical_analysis": (
                            "A child used the sandbox and filed the verified issue."
                        ),
                        "recommendations": "Confine resolved paths beneath the intended data root.",
                    }
                    async for item in self._tool("finish_scan", finish, "root-finish"):
                        yield item
                else:
                    command = {
                        "cmd": f"grep -n {_FIXTURE_MARKER} /workspace/{workspace_subdir}/app.py",
                        "yield_time_ms": 10000,
                    }
                    evidence = ""
                    async for item in self._tool("exec_command", command, "child-exec"):
                        if isinstance(item, UserMessage):
                            evidence = str(item.content[0].content)
                        yield item
                    report = {
                        "title": "Path traversal from unvalidated user path",
                        "description": "A user-controlled path escapes the intended data root.",
                        "impact": "An attacker can read files outside the intended directory.",
                        "target": f"/workspace/{workspace_subdir}/app.py",
                        "technical_analysis": "The path is joined without a containment check.",
                        "poc_description": "Pass ../secret.txt to read_file.",
                        "poc_script_code": "read_file('../secret.txt')",
                        "remediation_steps": "Resolve and reject paths outside DATA_ROOT.",
                        "evidence": evidence,
                        "assumptions": "The filename is attacker controlled.",
                        "fix_effort": "low",
                        "cvss_breakdown": {
                            "attack_vector": "L",
                            "attack_complexity": "L",
                            "privileges_required": "L",
                            "user_interaction": "N",
                            "scope": "U",
                            "confidentiality": "H",
                            "integrity": "N",
                            "availability": "N",
                        },
                        "cwe": "CWE-22",
                    }
                    async for item in self._tool(
                        "create_vulnerability_report", report, "child-report"
                    ):
                        yield item
                    done = {
                        "result_summary": "Confirmed and filed the fixture path traversal.",
                        "findings": ["CWE-22 path traversal"],
                        "success": True,
                        "report_to_parent": True,
                        "final_recommendations": ["Add a resolved-path containment check"],
                    }
                    async for item in self._tool("agent_finish", done, "child-finish"):
                        yield item
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id=self.session_id,
                    usage={"input_tokens": 0, "output_tokens": 0},
                    terminal_reason="completed",
                )
            finally:
                type(self).active -= 1

    return MultiAgentDryRunClient
