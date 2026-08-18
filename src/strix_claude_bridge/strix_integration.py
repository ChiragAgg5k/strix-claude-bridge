"""Narrow, version-gated seam between the companion package and Strix."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from strix_claude_bridge.backend import (
    AgentSessionSpec,
    SessionCheckpoint,
    SessionCompatibilityError,
)
from strix_claude_bridge.tool_adapter import (
    ToolContextBinding,
    build_strix_mcp_server,
    strict_strix_context_factory,
)

SUPPORTED_STRIX_VERSION = "1.5.3"
SUPPORTED_STRIX_COMMIT = "8ede419dccf6742aa0e0c4fe3e7faf11c471ff9a"
SUPPORTED_OPENAI_AGENTS_VERSION = "0.19.0"
SUPPORTED_CLAUDE_SDK_VERSION = "0.2.139"


class IntegrationCompatibilityError(RuntimeError):
    """Raised before runtime side effects when a pinned integration is incompatible."""


def _distribution_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError as exc:
        raise IntegrationCompatibilityError(
            f"required distribution {name!r} is not installed"
        ) from exc


def verify_runtime_compatibility(
    *,
    strix_version: str | None = None,
    sdk_version: str | None = None,
    openai_agents_version: str | None = None,
) -> None:
    """Fail fast unless all integration dependencies match the tested pins."""
    actual_strix = strix_version or _distribution_version("strix-agent")
    actual_sdk = sdk_version or _distribution_version("claude-agent-sdk")
    actual_openai_agents = openai_agents_version or _distribution_version("openai-agents")
    mismatches: list[str] = []
    if actual_strix != SUPPORTED_STRIX_VERSION:
        mismatches.append(f"strix-agent {actual_strix} (expected {SUPPORTED_STRIX_VERSION})")
    if actual_sdk != SUPPORTED_CLAUDE_SDK_VERSION:
        mismatches.append(
            f"claude-agent-sdk {actual_sdk} (expected {SUPPORTED_CLAUDE_SDK_VERSION})"
        )
    if actual_openai_agents != SUPPORTED_OPENAI_AGENTS_VERSION:
        mismatches.append(
            f"openai-agents {actual_openai_agents} (expected {SUPPORTED_OPENAI_AGENTS_VERSION})"
        )
    if mismatches:
        raise IntegrationCompatibilityError(
            "unsupported integration versions: " + "; ".join(mismatches)
        )


def verify_strix_source(source: Path) -> None:
    """Verify the exact source baseline used by the compatibility tests."""
    pyproject = source / "pyproject.toml"
    if not pyproject.is_file():
        raise IntegrationCompatibilityError("Strix source has no pyproject.toml")
    text = pyproject.read_text(encoding="utf-8")
    version_match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if version_match is None or version_match.group(1) != SUPPORTED_STRIX_VERSION:
        actual = version_match.group(1) if version_match else "unknown"
        raise IntegrationCompatibilityError(
            f"unsupported Strix source version {actual}; expected {SUPPORTED_STRIX_VERSION}"
        )
    try:
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrationCompatibilityError("could not identify Strix source commit") from exc
    if commit != SUPPORTED_STRIX_COMMIT:
        raise IntegrationCompatibilityError(
            f"unsupported Strix source commit {commit}; expected {SUPPORTED_STRIX_COMMIT}"
        )
    try:
        dirty = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrationCompatibilityError("could not inspect Strix source worktree") from exc
    if dirty:
        raise IntegrationCompatibilityError("Strix source worktree has uncommitted changes")


@dataclass(frozen=True)
class StrixAgentBridgeInput:
    """Backend-neutral data a small upstream Strix runner seam must provide."""

    agent_id: str
    system_prompt: str
    cwd: Path
    context: dict[str, Any]
    function_tools: Sequence[Any]
    turn_input: Sequence[Any] = ()
    turn_input_provider: Callable[[], Sequence[Any]] | None = None
    model: str | None = None
    max_turns: int | None = None
    resume_session_id: str | None = None
    turn_timeout_s: float | None = None
    resume_checkpoint: SessionCheckpoint | None = None
    journal: Any | None = None
    max_tool_calls: int | None = None
    parking_semaphore: Any | None = None


def build_claude_session_spec(value: StrixAgentBridgeInput) -> AgentSessionSpec:
    """Bind Strix host tools/context to one isolated per-agent MCP server."""
    verify_runtime_compatibility()
    binding = ToolContextBinding(
        agent_id=value.agent_id,
        context=value.context,
        turn_input=value.turn_input,
        turn_input_provider=value.turn_input_provider,
        journal=value.journal,
        max_tool_calls=value.max_tool_calls,
        parking_semaphore=value.parking_semaphore,
    )
    safe_agent_id = re.sub(r"[^A-Za-z0-9_-]", "_", value.agent_id)
    server_name = f"strix_{safe_agent_id}"
    server = build_strix_mcp_server(
        name=server_name,
        tools=value.function_tools,
        binding=binding,
        context_factory=strict_strix_context_factory,
    )
    if value.resume_checkpoint is not None:
        raise SessionCompatibilityError(
            "provider checkpoint resume is disabled until graph and tool reconciliation exist"
        )
    return AgentSessionSpec(
        agent_id=value.agent_id,
        system_prompt=value.system_prompt,
        cwd=value.cwd,
        mcp_servers={server_name: server},
        allowed_tools=server.allowed_tools,
        terminal_tools=tuple(
            name
            for name in server.allowed_tools
            if name.rsplit("__", 1)[-1] in {"finish_scan", "agent_finish"}
        ),
        model=value.model,
        max_turns=value.max_turns,
        resume_session_id=value.resume_session_id,
        resume_checkpoint=value.resume_checkpoint,
        turn_timeout_s=value.turn_timeout_s,
    )
