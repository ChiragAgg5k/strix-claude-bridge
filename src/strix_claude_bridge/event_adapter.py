"""Claude Agent SDK to backend-neutral event normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from strix_claude_bridge.backend import BackendEvent, BackendResult
from strix_claude_bridge.events import to_secret_safe


def normalize_usage(value: Any) -> dict[str, Any]:
    """Retain token/request metadata without representing subscription spend."""
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "service_tier",
    }
    return {key: to_secret_safe(item) for key, item in value.items() if key in allowed}


def normalize_model_identities(value: Any) -> tuple[str, ...]:
    """Return bounded provider-reported model identifiers without inventing defaults."""
    if not isinstance(value, Mapping):
        return ()
    models = {
        key.strip()
        for key in value
        if isinstance(key, str)
        and key.strip()
        and len(key.strip()) <= 200
        and all(character.isprintable() for character in key)
    }
    return tuple(sorted(models))


def normalize_sdk_message(message: Any, *, agent_id: str) -> list[BackendEvent]:
    """Translate pinned SDK dataclasses (or compatible fixtures) to stable events.

    Events containing transcript, tool, partial, or provider-system content are
    explicitly sensitive. General event sinks must not persist them by default.
    """
    message_type = type(message).__name__
    events: list[BackendEvent] = []

    if message_type == "AssistantMessage":
        error = getattr(message, "error", None)
        if error:
            events.append(BackendEvent("provider_error", agent_id, {"category": str(error)}))
        for block in getattr(message, "content", ()):
            block_type = type(block).__name__
            if block_type == "TextBlock":
                events.append(
                    BackendEvent(
                        "assistant_text",
                        agent_id,
                        {"text": getattr(block, "text", "")},
                        sensitive=True,
                    )
                )
            elif block_type in {"ToolUseBlock", "ServerToolUseBlock"}:
                events.append(
                    BackendEvent(
                        "tool_call",
                        agent_id,
                        {
                            "call_id": getattr(block, "id", None),
                            "name": getattr(block, "name", None),
                            "arguments": to_secret_safe(getattr(block, "input", {})),
                        },
                        sensitive=True,
                    )
                )
            elif block_type in {"ToolResultBlock", "ServerToolResultBlock"}:
                events.append(
                    BackendEvent(
                        "tool_result",
                        agent_id,
                        {
                            "call_id": getattr(block, "tool_use_id", None),
                            "content": to_secret_safe(getattr(block, "content", None)),
                            "is_error": bool(getattr(block, "is_error", False)),
                        },
                        sensitive=True,
                    )
                )
        return events

    if message_type == "UserMessage":
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return [BackendEvent("user_message", agent_id, {"text": content}, sensitive=True)]
        for block in content or ():
            if type(block).__name__ == "ToolResultBlock":
                events.append(
                    BackendEvent(
                        "tool_result",
                        agent_id,
                        {
                            "call_id": getattr(block, "tool_use_id", None),
                            "content": to_secret_safe(getattr(block, "content", None)),
                            "is_error": bool(getattr(block, "is_error", False)),
                        },
                        sensitive=True,
                    )
                )
        return events

    if message_type == "StreamEvent":
        return [
            BackendEvent(
                "partial",
                agent_id,
                {
                    "message_id": getattr(message, "uuid", None),
                    "event": to_secret_safe(getattr(message, "event", {})),
                },
                sensitive=True,
            )
        ]

    if message_type == "ResultMessage":
        result = result_from_sdk_message(message)
        return [
            BackendEvent(
                "terminal",
                agent_id,
                {
                    "terminal_reason": result.terminal_reason,
                    "is_error": result.is_error,
                    "turns": result.turns,
                    "usage": dict(result.usage),
                    "models": list(result.models),
                },
            )
        ]

    if message_type == "RateLimitEvent":
        info = getattr(message, "rate_limit_info", None)

        def value(name: str) -> Any:
            if isinstance(info, Mapping):
                return info.get(name)
            return getattr(info, name, None)

        payload = {
            key: to_secret_safe(value(key))
            for key in (
                "status",
                "resets_at",
                "rate_limit_type",
                "utilization",
                "overage_status",
                "overage_resets_at",
                "overage_disabled_reason",
            )
            if value(key) is not None
        }
        return [BackendEvent("rate_limit", agent_id, payload)]
    if message_type == "SystemMessage":
        return [
            BackendEvent(
                "system",
                agent_id,
                {
                    "subtype": getattr(message, "subtype", None),
                    "data": to_secret_safe(getattr(message, "data", {})),
                },
                sensitive=True,
            )
        ]
    return [BackendEvent("provider_event", agent_id, {"message_type": message_type})]


def result_from_sdk_message(message: Any) -> BackendResult:
    """Normalize a Claude terminal result while dropping provider dollar cost."""
    reason = (
        getattr(message, "terminal_reason", None)
        or getattr(message, "stop_reason", None)
        or getattr(message, "subtype", None)
        or "completed"
    )
    is_error = bool(
        getattr(message, "is_error", False)
        or getattr(message, "api_error_status", None) is not None
        or reason not in {"completed", "success"}
    )
    normalized_reason = str(reason)
    return BackendResult(
        provider_session_id=getattr(message, "session_id", None),
        terminal_reason=normalized_reason,
        is_error=is_error,
        usage=normalize_usage(getattr(message, "usage", None)),
        turns=getattr(message, "num_turns", None),
        models=normalize_model_identities(getattr(message, "model_usage", None)),
    )


def to_strix_stream_event(event: BackendEvent) -> dict[str, Any]:
    """Project a bridge event into the legacy Strix transcript/viewer shape.

    This mirror is for rendering/reporting only; it is never replayed into
    Claude for inference resume.
    """
    payload = dict(event.payload)
    if event.kind == "assistant_text":
        return {
            "type": "raw_response_event",
            "data": {"type": "response.output_text.delta", "delta": payload.get("text", "")},
            "agent_id": event.agent_id,
            "sequence": event.sequence,
        }
    if event.kind == "tool_call":
        return {
            "type": "run_item_stream_event",
            "name": "tool_called",
            "item": {
                "type": "tool_call_item",
                "call_id": payload.get("call_id"),
                "name": payload.get("name"),
                "arguments": payload.get("arguments", {}),
            },
            "agent_id": event.agent_id,
            "sequence": event.sequence,
        }
    if event.kind == "tool_result":
        return {
            "type": "run_item_stream_event",
            "name": "tool_output",
            "item": {
                "type": "tool_call_output_item",
                "call_id": payload.get("call_id"),
                "output": payload.get("content"),
                "is_error": bool(payload.get("is_error", False)),
            },
            "agent_id": event.agent_id,
            "sequence": event.sequence,
        }
    return {
        "type": "bridge_backend_event",
        "name": event.kind,
        "agent_id": event.agent_id,
        "sequence": event.sequence,
        "data": payload,
    }
