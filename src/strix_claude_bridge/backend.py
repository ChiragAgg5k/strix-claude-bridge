"""Backend-neutral execution and durable session contracts."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any


class BackendStateError(RuntimeError):
    """Raised when a session lifecycle method is called in the wrong state."""


class SessionCompatibilityError(RuntimeError):
    """Raised when durable provider state is unsafe to resume."""


@dataclass(frozen=True)
class BackendEvent:
    """Versioned event emitted by every execution backend.

    Content-bearing events are marked sensitive so persistence sinks can fail
    closed unless the local operator has explicitly enabled transcript storage.
    """

    kind: str
    agent_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    sequence: int = 0
    schema_version: int = 1
    sensitive: bool = False


@dataclass(frozen=True)
class BackendResult:
    """Terminal information for one provider turn."""

    provider_session_id: str | None
    terminal_reason: str
    is_error: bool
    usage: Mapping[str, Any] = field(default_factory=dict)
    turns: int | None = None
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class InProcessMCPServer:
    """Vetted SDK in-process server plus the only tools it may expose."""

    name: str
    configuration: Mapping[str, Any]
    allowed_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        configuration = MappingProxyType(dict(self.configuration))
        allowed_tools = tuple(self.allowed_tools)
        object.__setattr__(self, "configuration", configuration)
        object.__setattr__(self, "allowed_tools", allowed_tools)
        if not self.name or configuration.get("type") != "sdk":
            raise ValueError("only named SDK in-process MCP servers are supported")
        if configuration.get("name") != self.name:
            raise ValueError("MCP server configuration name mismatch")
        instance = configuration.get("instance")
        if instance is None or type(instance).__module__ != "mcp.server.lowlevel.server":
            raise ValueError("only SDK-created in-process MCP server instances are supported")
        expected_prefix = f"mcp__{self.name}__"
        if not allowed_tools or any(
            not isinstance(item, str) or not item.startswith(expected_prefix)
            for item in allowed_tools
        ):
            raise ValueError("MCP allowed tools must belong to their in-process server")
        if len(allowed_tools) != len(set(allowed_tools)):
            raise ValueError("MCP allowed tools must be unique")


@dataclass(frozen=True)
class AgentSessionSpec:
    """Backend-neutral description of one Strix agent session."""

    agent_id: str
    system_prompt: str
    cwd: Path
    mcp_servers: Mapping[str, InProcessMCPServer]
    allowed_tools: tuple[str, ...]
    model: str | None = None
    max_turns: int | None = None
    resume_session_id: str | None = None
    resume_checkpoint: SessionCheckpoint | None = None
    turn_timeout_s: float | None = None
    terminal_tools: tuple[str, ...] = ()
    authentication_mode: str = "claude_subscription"

    def __post_init__(self) -> None:
        mcp_servers = MappingProxyType(dict(self.mcp_servers))
        allowed_tools = tuple(self.allowed_tools)
        terminal_tools = tuple(self.terminal_tools)
        object.__setattr__(self, "mcp_servers", mcp_servers)
        object.__setattr__(self, "allowed_tools", allowed_tools)
        object.__setattr__(self, "terminal_tools", terminal_tools)
        if not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if not self.system_prompt.strip():
            raise ValueError("system_prompt must not be empty")
        if self.authentication_mode != "claude_subscription":
            raise ValueError("only explicit claude_subscription authentication is supported")
        if self.resume_session_id is not None:
            raise SessionCompatibilityError(
                "raw provider resume is disabled; provide a validated checkpoint"
            )
        if self.resume_checkpoint is not None:
            raise SessionCompatibilityError(
                "provider checkpoint resume is disabled until graph and tool reconciliation exist"
            )
        if not mcp_servers:
            raise ValueError("at least one vetted in-process MCP server is required")
        configured: list[str] = []
        for name, server in mcp_servers.items():
            if name != server.name:
                raise ValueError("MCP server mapping key does not match its vetted name")
            configured.extend(server.allowed_tools)
        if tuple(configured) != allowed_tools:
            raise ValueError("allowed_tools must exactly match the vetted MCP server tools")
        if not allowed_tools:
            raise ValueError("at least one explicitly allowed MCP tool is required")
        if any(name not in allowed_tools for name in terminal_tools):
            raise ValueError("terminal tools must belong to the exact MCP allowlist")
        if len(terminal_tools) != len(set(terminal_tools)):
            raise ValueError("terminal tools must be unique")
        if self.max_turns is not None and self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.turn_timeout_s is not None and self.turn_timeout_s <= 0:
            raise ValueError("turn_timeout_s must be positive")


@dataclass(frozen=True)
class SessionCheckpoint:
    """Sensitive provider capability metadata for a restricted checkpoint store.

    The opaque provider session identifier is in-memory execution authority. It
    must not appear in general logs or durable state; ``to_audit_dict`` replaces
    it with a one-way hash. Process-restart checkpoint resume is disabled.
    """

    provider_backend: str
    backend_version: str
    sdk_version: str
    model: str | None
    tool_schema_digest: str
    provider_session_id: str
    cwd_identity: str
    last_settled_turn: int
    cli_version: str = "unknown"
    settled: bool = True
    journal_clean: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        required_strings = (
            self.provider_backend,
            self.backend_version,
            self.sdk_version,
            self.tool_schema_digest,
            self.provider_session_id,
            self.cwd_identity,
        )
        if any(not isinstance(value, str) or not value for value in required_strings):
            raise ValueError("checkpoint identifiers must be non-empty strings")
        if self.schema_version != 1 or self.last_settled_turn < 0:
            raise ValueError("unsupported backend session checkpoint")
        if not isinstance(self.cli_version, str) or not self.cli_version:
            raise ValueError("checkpoint CLI version must be a non-empty string")

    def to_audit_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        provider_session_id = payload.pop("provider_session_id")
        payload["provider_session_id_sha256"] = hashlib.sha256(
            provider_session_id.encode()
        ).hexdigest()
        return payload

    def require_compatible(
        self,
        *,
        provider_backend: str,
        backend_version: str,
        sdk_version: str,
        tool_schema_digest: str,
        cwd_identity: str,
    ) -> None:
        expected = {
            "provider_backend": provider_backend,
            "backend_version": backend_version,
            "sdk_version": sdk_version,
            "tool_schema_digest": tool_schema_digest,
            "cwd_identity": cwd_identity,
        }
        mismatches = [name for name, wanted in expected.items() if getattr(self, name) != wanted]
        if mismatches:
            raise SessionCompatibilityError(
                "incompatible backend session checkpoint fields: " + ", ".join(mismatches)
            )


def tool_schema_digest(schemas: Mapping[str, Mapping[str, Any]]) -> str:
    """Return a deterministic compatibility digest for a complete tool set."""
    encoded = json.dumps(schemas, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class ExecutionSession(ABC):
    """One provider-owned agent loop and its reusable session lifecycle."""

    @abstractmethod
    async def start(self, initial_input: str) -> None:
        """Connect and begin the first turn."""

    @abstractmethod
    def events(self) -> AsyncIterator[BackendEvent]:
        """Stream normalized events for the active turn."""

    @abstractmethod
    async def continue_with(self, input_text: str) -> None:
        """Begin another turn after the prior event stream has settled."""

    @abstractmethod
    async def inject(self, input_text: str) -> None:
        """Inject an out-of-band coordinator message as a new provider query."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Interrupt the active provider turn without discarding the session."""

    @abstractmethod
    async def cancel(self) -> None:
        """Cancel this agent and release its provider resources."""

    @abstractmethod
    async def close(self) -> None:
        """Idempotently release provider resources."""

    @property
    @abstractmethod
    def result(self) -> BackendResult | None:
        """Return terminal information for the most recently settled turn."""


class ExecutionBackend(ABC):
    """Factory boundary selected by Strix instead of a LiteLLM model name."""

    @abstractmethod
    def create_session(self, spec: AgentSessionSpec) -> ExecutionSession:
        """Create one independent provider session for one Strix agent."""
