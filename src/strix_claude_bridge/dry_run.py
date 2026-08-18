"""Deterministic fake Agent SDK client for the credential-free Strix dry-run."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from mcp.types import CallToolRequest, CallToolRequestParams

_FIXTURE_MARKER = "STRIX_DRY_RUN_PATH_TRAVERSAL"


def _response_text(response: Any) -> str:
    root = getattr(response, "root", response)
    content = getattr(root, "content", ())
    return "\n".join(
        text for block in content if isinstance((text := getattr(block, "text", None)), str)
    )


def build_dry_run_client_factory(*, workspace_subdir: str) -> Callable[[Any], Any]:
    """Return a scripted client that crosses the real SDK MCP control protocol."""
    if not workspace_subdir or "/" in workspace_subdir or ".." in workspace_subdir:
        raise ValueError("dry-run workspace subdirectory is invalid")

    class DryRunClaudeSDKClient:
        def __init__(self, options: Any) -> None:
            self.options = options
            self.connected = False
            self.query_text = ""
            server = next(iter(options.mcp_servers.values()))
            self.instance = server["instance"]
            self.server_name = server["name"]

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def interrupt(self) -> None:
            return

        async def query(self, prompt: str) -> None:
            self.query_text = prompt

        async def _call(self, name: str, arguments: dict[str, Any]) -> str:
            request = CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments))
            response = await self.instance.request_handlers[CallToolRequest](request)
            if bool(getattr(response.root, "isError", False)):
                raise RuntimeError(f"dry-run tool {name} returned an MCP error")
            return _response_text(response)

        async def receive_response(self):
            exec_id = "dry-run-exec"
            exec_arguments = {
                "cmd": (f"grep -n {_FIXTURE_MARKER} /workspace/{workspace_subdir}/app.py"),
                "yield_time_ms": 10000,
            }
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        exec_id,
                        f"mcp__{self.server_name}__exec_command",
                        exec_arguments,
                    )
                ],
                model="fake-agent-sdk",
            )
            evidence = await self._call("exec_command", exec_arguments)
            if _FIXTURE_MARKER not in evidence:
                raise RuntimeError(
                    "dry-run fixture marker was not observed through Strix exec_command"
                )
            yield UserMessage(
                content=[ToolResultBlock(exec_id, evidence, False)],
            )

            report_id = "dry-run-report"
            report_arguments = {
                "title": "Path traversal from unvalidated user path",
                "description": "A user-controlled path is joined to the application data root.",
                "impact": "An attacker can read files outside the intended data directory.",
                "target": f"/workspace/{workspace_subdir}/app.py",
                "technical_analysis": (
                    "The vulnerable function joins a caller-controlled path without confinement."
                ),
                "poc_description": "Pass ../secret.txt to the fixture read_file function.",
                "poc_script_code": "read_file('../secret.txt')",
                "remediation_steps": (
                    "Resolve the candidate path and reject it unless it remains under DATA_ROOT."
                ),
                "evidence": evidence,
                "assumptions": "The attacker can control the filename argument.",
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
                "code_locations": [
                    {
                        "file": "app.py",
                        "start_line": 9,
                        "end_line": 10,
                        "snippet": "return (DATA_ROOT / user_path).read_text()",
                    }
                ],
            }
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        report_id,
                        f"mcp__{self.server_name}__create_vulnerability_report",
                        report_arguments,
                    )
                ],
                model="fake-agent-sdk",
            )
            report_result = await self._call("create_vulnerability_report", report_arguments)
            if '"success": true' not in report_result:
                raise RuntimeError("dry-run Strix vulnerability report was not created")
            yield UserMessage(content=[ToolResultBlock(report_id, report_result, False)])

            finish_id = "dry-run-finish"
            finish_arguments = {
                "executive_summary": (
                    "The deterministic authorized fixture contains one confirmed path traversal."
                ),
                "methodology": (
                    "Inspected the mounted fixture through the real Strix Docker shell tool and "
                    "filed the verified result through Strix reporting."
                ),
                "technical_analysis": (
                    "Untrusted path input reaches pathlib.Path joining without a containment check."
                ),
                "recommendations": (
                    "Resolve and confine file paths beneath the intended data directory."
                ),
            }
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        finish_id,
                        f"mcp__{self.server_name}__finish_scan",
                        finish_arguments,
                    )
                ],
                model="fake-agent-sdk",
            )
            finish_result = await self._call("finish_scan", finish_arguments)
            if '"scan_completed": true' not in finish_result:
                raise RuntimeError("dry-run Strix finish_scan did not complete")
            yield UserMessage(content=[ToolResultBlock(finish_id, finish_result, False)])
            yield AssistantMessage(
                content=[TextBlock("Deterministic fake inference completed the scripted fixture.")],
                model="fake-agent-sdk",
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=0,
                is_error=False,
                num_turns=1,
                session_id="simulated-dry-run-session",
                usage={"input_tokens": 0, "output_tokens": 0},
                terminal_reason="completed",
            )

    return DryRunClaudeSDKClient
