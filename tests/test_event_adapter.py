from __future__ import annotations

from claude_agent_sdk.types import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from strix_claude_bridge.backend import BackendEvent
from strix_claude_bridge.event_adapter import (
    normalize_sdk_message,
    result_from_sdk_message,
    to_strix_stream_event,
)


def test_assistant_tool_and_text_events_are_normalized() -> None:
    message = AssistantMessage(
        content=[
            TextBlock("checking"),
            ToolUseBlock("call-1", "mcp__strix_root__notes", {"title": "x"}),
            ToolResultBlock("call-1", "ok", False),
        ],
        model="claude-sonnet",
    )

    events = normalize_sdk_message(message, agent_id="root")

    assert [event.kind for event in events] == ["assistant_text", "tool_call", "tool_result"]
    assert events[1].payload == {
        "call_id": "call-1",
        "name": "mcp__strix_root__notes",
        "arguments": {"title": "x"},
    }
    assert events[2].payload["is_error"] is False
    assert all(event.sensitive for event in events)


def test_partial_and_terminal_events_have_stable_backend_shape() -> None:
    partial = StreamEvent(
        uuid="message-1",
        session_id="session-1",
        event={"type": "content_block_delta", "delta": {"text": "part"}},
    )
    terminal = ResultMessage(
        subtype="success",
        duration_ms=2,
        duration_api_ms=1,
        is_error=False,
        num_turns=3,
        session_id="session-1",
        usage={
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 2,
            "total_cost_usd": 99,
        },
        total_cost_usd=99,
        terminal_reason="completed",
        model_usage={
            "claude-sonnet-4-5-20250929": {
                "inputTokens": 10,
                "outputTokens": 4,
                "cacheReadInputTokens": 2,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 99,
                "contextWindow": 200000,
                "maxOutputTokens": 64000,
            }
        },
    )

    partial_event = normalize_sdk_message(partial, agent_id="root")[0]
    terminal_event = normalize_sdk_message(terminal, agent_id="root")[0]
    result = result_from_sdk_message(terminal)

    assert partial_event.kind == "partial"
    assert partial_event.payload["message_id"] == "message-1"
    assert terminal_event.kind == "terminal"
    assert "provider_session_id" not in terminal_event.payload
    assert partial_event.sensitive is True
    assert terminal_event.sensitive is False
    assert terminal_event.payload["usage"] == {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_read_input_tokens": 2,
    }
    assert result.is_error is False
    assert result.models == ("claude-sonnet-4-5-20250929",)
    assert terminal_event.payload["models"] == ["claude-sonnet-4-5-20250929"]
    assert "total_cost_usd" not in result.usage


def test_rate_limit_event_keeps_status_but_drops_provider_identifiers_and_raw_data() -> None:
    session_secret = "provider-session-secret"
    uuid_secret = "provider-event-secret"
    raw_secret = "raw-provider-secret"
    message = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed",
            resets_at=123,
            rate_limit_type="five_hour",
            utilization=0.25,
            overage_status="rejected",
            overage_disabled_reason="out_of_credits",
            raw={"secret": raw_secret},
        ),
        uuid=uuid_secret,
        session_id=session_secret,
    )

    event = normalize_sdk_message(message, agent_id="root")[0]
    serialized = repr(event.payload)

    assert event.kind == "rate_limit"
    assert event.sensitive is False
    assert event.payload == {
        "status": "allowed",
        "resets_at": 123,
        "rate_limit_type": "five_hour",
        "utilization": 0.25,
        "overage_status": "rejected",
        "overage_disabled_reason": "out_of_credits",
    }
    assert session_secret not in serialized
    assert uuid_secret not in serialized
    assert raw_secret not in serialized


def test_default_model_is_not_invented_when_provider_omits_model_usage() -> None:
    terminal = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-default",
        usage={"input_tokens": 1, "output_tokens": 1},
        terminal_reason="completed",
    )

    result = result_from_sdk_message(terminal)

    assert result.models == ()


def test_bridge_events_project_to_strix_viewer_shapes() -> None:
    text = to_strix_stream_event(
        BackendEvent("assistant_text", "root", {"text": "delta"}, sequence=1)
    )
    call = to_strix_stream_event(
        BackendEvent(
            "tool_call",
            "root",
            {"call_id": "c1", "name": "exec_command", "arguments": {"cmd": "id"}},
            sequence=2,
        )
    )
    output = to_strix_stream_event(
        BackendEvent(
            "tool_result",
            "root",
            {"call_id": "c1", "content": "ok", "is_error": False},
            sequence=3,
        )
    )

    assert text["data"] == {"type": "response.output_text.delta", "delta": "delta"}
    assert call["item"]["type"] == "tool_call_item"
    assert output["item"]["type"] == "tool_call_output_item"
