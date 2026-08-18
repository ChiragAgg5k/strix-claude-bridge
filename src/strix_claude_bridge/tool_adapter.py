"""Generic Strix/OpenAI function-tool to Claude SDK MCP adapter."""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk import tool as sdk_tool

from strix_claude_bridge.backend import InProcessMCPServer

_IMAGE_URI = re.compile(
    r"^data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/]+={0,2})$",
    re.IGNORECASE,
)
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


class ToolCompatibilityError(RuntimeError):
    """Raised when a Strix tool cannot be represented safely as an MCP tool."""


TurnInputProvider = Callable[[], Sequence[Any]]


@dataclass
class _ToolCallBudget:
    count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class ToolContextBinding:
    """Host-selected context captured by one agent's in-process MCP server."""

    agent_id: str
    context: Mapping[str, Any]
    turn_input: Sequence[Any] = field(default_factory=tuple)
    turn_input_provider: TurnInputProvider | None = None
    journal: Any | None = None
    max_tool_calls: int | None = None
    parking_semaphore: asyncio.Semaphore | None = None
    _tool_budget: _ToolCallBudget = field(
        default_factory=_ToolCallBudget, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.agent_id:
            raise ValueError("agent_id must not be empty")
        immutable_context = MappingProxyType(dict(self.context))
        immutable_turn_input = tuple(self.turn_input)
        object.__setattr__(self, "context", immutable_context)
        object.__setattr__(self, "turn_input", immutable_turn_input)
        if self.max_tool_calls is not None and self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        context_agent_id = immutable_context.get("agent_id")
        if context_agent_id is not None and context_agent_id != self.agent_id:
            raise ValueError("context agent_id does not match the immutable binding")

    def current_turn_input(self) -> tuple[Any, ...]:
        """Return the host-controlled snapshot used for the current tool call."""
        value = self.turn_input if self.turn_input_provider is None else self.turn_input_provider()
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ToolCompatibilityError("turn input provider must return a sequence")
        return tuple(value)


@dataclass
class ToolContextShim:
    """Small OpenAI-Agents-compatible context used by existing Strix tools."""

    context: Mapping[str, Any]
    turn_input: list[Any]
    tool_name: str
    tool_call_id: str
    tool_arguments: str
    usage: Any = field(default_factory=SimpleNamespace)
    tool_input: Any | None = None


ContextFactory = Callable[[ToolContextBinding, str, str, str], Any]


def default_context_factory(
    binding: ToolContextBinding,
    tool_name: str,
    raw_arguments: str,
    call_id: str,
) -> Any:
    """Build a context without requiring OpenAI Agents at bridge import time."""
    try:
        from agents.tool_context import ToolContext
    except ImportError:
        return ToolContextShim(
            context=dict(binding.context),
            turn_input=list(binding.current_turn_input()),
            tool_name=tool_name,
            tool_call_id=call_id,
            tool_arguments=raw_arguments,
        )
    return ToolContext(
        context=dict(binding.context),
        tool_name=tool_name,
        tool_call_id=call_id,
        tool_arguments=raw_arguments,
        turn_input=list(binding.current_turn_input()),
    )


def strict_strix_context_factory(
    binding: ToolContextBinding,
    tool_name: str,
    raw_arguments: str,
    call_id: str,
) -> Any:
    """Build the pinned real ToolContext or fail closed in the Strix seam."""
    try:
        from agents.tool_context import ToolContext
    except ImportError as exc:  # pragma: no cover - guarded by pinned integration extras
        raise ToolCompatibilityError("pinned OpenAI Agents ToolContext is unavailable") from exc
    return ToolContext(
        context=dict(binding.context),
        tool_name=tool_name,
        tool_call_id=call_id,
        tool_arguments=raw_arguments,
        turn_input=list(binding.current_turn_input()),
    )


def convert_tool_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Copy and validate an OpenAI function schema for MCP registration."""
    converted = copy.deepcopy(dict(schema))
    if converted.get("type") != "object" or not isinstance(converted.get("properties"), dict):
        raise ToolCompatibilityError("function tool schema must be a JSON Schema object")
    required = converted.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ToolCompatibilityError("function tool schema required field must be a string list")
    return converted


def _matches_image_signature(mime_type: str, decoded: bytes) -> bool:
    signatures = {
        "image/png": lambda: decoded.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": lambda: decoded.startswith(b"\xff\xd8\xff"),
        "image/gif": lambda: decoded.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": lambda: (
            len(decoded) >= 12 and decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP"
        ),
    }
    return signatures[mime_type]()


def _image_content(image_url: str) -> dict[str, str] | None:
    match = _IMAGE_URI.fullmatch(image_url.strip())
    if match is None:
        return None
    mime_type = match.group(1).lower()
    try:
        decoded = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error):
        return None
    if (
        not decoded
        or len(decoded) > _MAX_IMAGE_BYTES
        or not _matches_image_signature(mime_type, decoded)
    ):
        return None
    return {"type": "image", "data": match.group(2), "mimeType": mime_type}


def _content_block(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        return {"type": "text", "text": value}
    if isinstance(value, Mapping):
        block_type = value.get("type")
        if block_type == "text" and isinstance(value.get("text"), str):
            return {"type": "text", "text": value["text"]}
        if (
            block_type == "image"
            and isinstance(value.get("data"), str)
            and isinstance(value.get("mimeType"), str)
        ):
            return _image_content(f"data:{value['mimeType']};base64,{value['data']}")
        image_url = value.get("image_url") or value.get("url")
        if isinstance(image_url, str):
            return _image_content(image_url)
    block_type = getattr(value, "type", None)
    text = getattr(value, "text", None)
    if block_type == "text" and isinstance(text, str):
        return {"type": "text", "text": text}
    data = getattr(value, "data", None)
    mime_type = getattr(value, "mimeType", None)
    if block_type == "image" and isinstance(data, str) and isinstance(mime_type, str):
        return _image_content(f"data:{mime_type};base64,{data}")
    image_url = getattr(value, "image_url", None)
    if isinstance(image_url, str):
        return _image_content(image_url)
    return None


def normalize_tool_result(result: Any) -> dict[str, Any]:
    """Normalize Strix text, structured, and image results to MCP content."""
    if isinstance(result, bytes):
        raise ToolCompatibilityError("binary tool results require an explicit image data URI")
    if isinstance(result, Mapping) and isinstance(result.get("content"), list):
        content = [_content_block(block) for block in result["content"]]
        if any(block is None for block in content):
            raise ToolCompatibilityError("tool returned an unsupported MCP content block")
        return {"content": content, "is_error": bool(result.get("is_error", False))}

    values = result if isinstance(result, list | tuple) else [result]
    blocks = [_content_block(value) for value in values]
    if all(block is not None for block in blocks):
        return {"content": blocks, "is_error": False}
    try:
        text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ToolCompatibilityError(
            f"unsupported tool result type: {type(result).__name__}"
        ) from exc
    return {"content": [{"type": "text", "text": text}], "is_error": False}


def _side_effecting_tool(name: str) -> bool:
    read_only = {
        "think",
        "load_skill",
        "web_search",
        "scope_rules",
        "view_image",
    }
    return name not in read_only and not name.startswith(("get_", "list_", "read_", "view_"))


def _tool_timeout(tool_object: Any) -> float | None:
    timeout = getattr(tool_object, "timeout_seconds", None)
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ToolCompatibilityError("tool timeout_seconds must be positive")
    return float(timeout)


def adapt_function_tool(
    tool_object: Any,
    binding: ToolContextBinding,
    *,
    context_factory: ContextFactory = default_context_factory,
) -> Any:
    """Adapt one FunctionTool-like object to a pinned SDK in-process MCP tool.

    The model supplies only schema arguments. Agent identity and all privileged
    services come from the immutable host binding captured by the handler.
    """
    name = getattr(tool_object, "name", None)
    description = getattr(tool_object, "description", None)
    invoke = getattr(tool_object, "on_invoke_tool", None)
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(description, str)
        or not callable(invoke)
    ):
        raise ToolCompatibilityError("expected a FunctionTool-like object")
    is_enabled = getattr(tool_object, "is_enabled", True)
    needs_approval = getattr(tool_object, "needs_approval", False)
    if is_enabled is not True:
        raise ToolCompatibilityError(
            f"tool {name!r} has unsupported dynamic or disabled enablement"
        )
    if needs_approval is not False:
        raise ToolCompatibilityError(f"tool {name!r} requires an unsupported approval contract")
    schema = convert_tool_schema(getattr(tool_object, "params_json_schema", {}))
    timeout_s = _tool_timeout(tool_object)

    @sdk_tool(name, description, schema)
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        raw_arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        async with binding._tool_budget.lock:
            binding._tool_budget.count += 1
            invocation_count = binding._tool_budget.count
            if binding.max_tool_calls is not None and invocation_count > binding.max_tool_calls:
                return {
                    "content": [{"type": "text", "text": "agent tool-call limit exhausted"}],
                    "is_error": True,
                }
            call_id = f"bridge-{binding.agent_id}-{invocation_count}"
        decision = (
            binding.journal.begin(binding.agent_id, name, raw_arguments)
            if binding.journal is not None and _side_effecting_tool(name)
            else None
        )
        journal_id = decision.invocation_id if decision is not None else None
        context = context_factory(binding, name, raw_arguments, call_id)
        parked = name == "wait_for_agents" and binding.parking_semaphore is not None
        if parked:
            # wait_for_agents is a coordinator wait, not active inference. Give the
            # scan slot to children (including recursively-created children) while
            # the provider tool call is parked, then balance the runner's lease.
            binding.parking_semaphore.release()
        try:
            pending = invoke(context, raw_arguments)
            result = (
                await asyncio.wait_for(pending, timeout=timeout_s)
                if timeout_s is not None
                else await pending
            )
            normalized = normalize_tool_result(result)
            if journal_id is not None:
                binding.journal.complete(journal_id, normalized)
            return normalized
        except asyncio.CancelledError:
            # Deliberately leave the write-ahead entry started. A later resume
            # must block rather than guessing whether the side effect happened.
            raise
        except TimeoutError:
            # A timeout cannot prove whether a side effect happened. Keep the
            # write-ahead entry started so explicit resume fails closed.
            message = (
                f"{name} timed out after {timeout_s:g}s"
                if timeout_s is not None
                else f"{name} failed (reference {call_id})"
            )
            return {
                "content": [{"type": "text", "text": message}],
                "is_error": True,
            }
        except Exception:
            # Tool exceptions can occur after a partial side effect. Preserve
            # the indeterminate started entry instead of permitting replay.
            return {
                "content": [{"type": "text", "text": f"{name} failed (reference {call_id})"}],
                "is_error": True,
            }
        finally:
            if parked:
                reacquire = asyncio.create_task(binding.parking_semaphore.acquire())
                try:
                    await asyncio.shield(reacquire)
                except asyncio.CancelledError:
                    # Balance the outer semaphore context before propagating
                    # cancellation; otherwise it would release a second permit.
                    await reacquire
                    raise

    return handler


def build_strix_mcp_server(
    *,
    name: str,
    tools: Sequence[Any],
    binding: ToolContextBinding,
    version: str = "1",
    context_factory: ContextFactory = default_context_factory,
) -> InProcessMCPServer:
    """Build one vetted per-agent MCP server and its exact Claude allowlist."""
    if not name or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("MCP server name must contain only letters, digits, '_' or '-'")
    adapted = [
        adapt_function_tool(item, binding, context_factory=context_factory) for item in tools
    ]
    names = [item.name for item in adapted]
    if len(names) != len(set(names)):
        raise ToolCompatibilityError("Strix tools must have unique names")
    configuration = create_sdk_mcp_server(name=name, version=version, tools=adapted)
    allowed = tuple(f"mcp__{name}__{tool_name}" for tool_name in names)
    return InProcessMCPServer(name=name, configuration=configuration, allowed_tools=allowed)
