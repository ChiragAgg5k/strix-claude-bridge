from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from strix_claude_bridge.tool_adapter import (
    ToolCompatibilityError,
    ToolContextBinding,
    adapt_function_tool,
    convert_tool_schema,
    normalize_tool_result,
)


@dataclass
class FakeFunctionTool:
    name: str
    description: str
    params_json_schema: dict[str, Any]
    on_invoke_tool: Any
    timeout_seconds: float | None = None
    is_enabled: Any = True
    needs_approval: Any = False


def schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "string"}},
            "options": {"type": "object", "additionalProperties": {"type": "boolean"}},
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def test_schema_conversion_preserves_structure_without_aliasing() -> None:
    original = schema()

    converted = convert_tool_schema(original)
    converted["properties"]["items"]["items"]["type"] = "integer"

    assert original["properties"]["items"]["items"]["type"] == "string"
    assert converted["required"] == ["items"]
    with pytest.raises(ToolCompatibilityError, match="JSON Schema object"):
        convert_tool_schema({"type": "string"})


@pytest.mark.asyncio
async def test_structured_arguments_and_context_are_bound_by_host() -> None:
    captured: dict[str, Any] = {}

    async def invoke(context: Any, raw: str) -> str:
        captured.update(
            context=context,
            arguments=json.loads(raw),
        )
        return '{"success":true}'

    function_tool = FakeFunctionTool("notes", "Create notes", schema(), invoke)
    source_context = {"agent_id": "root", "sandbox_session": object()}
    binding = ToolContextBinding(
        agent_id="root",
        context=source_context,
        turn_input=("prior",),
    )
    source_context["agent_id"] = "attacker-controlled"
    adapted = adapt_function_tool(function_tool, binding)

    result = await adapted.handler({"items": ["a"], "options": {"private": True}})

    assert captured["arguments"] == {"items": ["a"], "options": {"private": True}}
    assert captured["context"].context["agent_id"] == "root"
    assert captured["context"].turn_input == ["prior"]
    assert captured["context"].tool_call_id == "bridge-root-1"
    assert result == {
        "content": [{"type": "text", "text": '{"success":true}'}],
        "is_error": False,
    }
    with pytest.raises(ValueError, match="immutable binding"):
        ToolContextBinding(agent_id="child", context={"agent_id": "root"})


def test_result_normalization_handles_json_images_and_binary_rejection() -> None:
    structured = normalize_tool_result({"success": True, "count": 2})
    image_data = base64.b64encode(b"\x89PNG\r\n\x1a\nsmall-fixture").decode()
    image = normalize_tool_result(
        {"content": [{"image_url": f"data:image/png;base64,{image_data}"}]}
    )

    assert json.loads(structured["content"][0]["text"]) == {"count": 2, "success": True}
    assert image == {
        "content": [{"type": "image", "data": image_data, "mimeType": "image/png"}],
        "is_error": False,
    }
    with pytest.raises(ToolCompatibilityError, match="binary"):
        normalize_tool_result(b"raw")
    mismatched = base64.b64encode(b"not-a-png").decode()
    with pytest.raises(ToolCompatibilityError, match="unsupported MCP content"):
        normalize_tool_result({"content": [{"image_url": f"data:image/png;base64,{mismatched}"}]})


def test_standard_text_and_mcp_content_objects_are_supported() -> None:
    from mcp.types import TextContent

    openai_text = SimpleNamespace(type="text", text="openai text")
    mcp_text = TextContent(type="text", text="mcp text")

    assert normalize_tool_result([openai_text, mcp_text]) == {
        "content": [
            {"type": "text", "text": "openai text"},
            {"type": "text", "text": "mcp text"},
        ],
        "is_error": False,
    }


@pytest.mark.asyncio
async def test_errors_and_timeouts_are_model_visible() -> None:
    async def fail(_context: Any, _raw: str) -> str:
        raise RuntimeError("bounded failure")

    async def hang(_context: Any, _raw: str) -> str:
        await asyncio.sleep(60)
        return "late"

    binding = ToolContextBinding("root", {"agent_id": "root"})
    failed = adapt_function_tool(FakeFunctionTool("fail", "Fail", schema(), fail), binding)
    timed = adapt_function_tool(
        FakeFunctionTool("timed", "Timed", schema(), hang, timeout_seconds=0.01), binding
    )

    failed_result = await failed.handler({"items": []})
    timed_result = await timed.handler({"items": []})

    assert failed_result["is_error"] is True
    assert "bounded failure" not in failed_result["content"][0]["text"]
    assert failed_result["content"][0]["text"] == "fail failed (reference bridge-root-1)"
    assert timed_result["is_error"] is True
    assert timed_result["content"][0]["text"] == "timed timed out after 0.01s"


@pytest.mark.asyncio
async def test_tool_cancellation_propagates_to_original_tool() -> None:
    cancelled = asyncio.Event()

    async def hang(_context: Any, _raw: str) -> str:
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    adapted = adapt_function_tool(
        FakeFunctionTool("cancel", "Cancel", schema(), hang),
        ToolContextBinding("root", {"agent_id": "root"}),
    )
    task = asyncio.create_task(adapted.handler({"items": []}))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


def test_disabled_dynamic_and_approval_tools_fail_closed() -> None:
    async def invoke(_context: Any, _raw: str) -> str:
        return "unsafe"

    binding = ToolContextBinding("root", {"agent_id": "root"})
    disabled = FakeFunctionTool("disabled", "Disabled", schema(), invoke, is_enabled=False)
    dynamic = FakeFunctionTool("dynamic", "Dynamic", schema(), invoke, is_enabled=lambda _ctx: True)
    approval = FakeFunctionTool("approval", "Approval", schema(), invoke, needs_approval=True)

    for item in (disabled, dynamic):
        with pytest.raises(ToolCompatibilityError, match="enablement"):
            adapt_function_tool(item, binding)
    with pytest.raises(ToolCompatibilityError, match="approval"):
        adapt_function_tool(approval, binding)


@pytest.mark.asyncio
async def test_tool_limit_is_shared_across_all_agent_tools() -> None:
    async def invoke(_context: Any, _raw: str) -> str:
        return "ok"

    binding = ToolContextBinding("root", {"agent_id": "root"}, max_tool_calls=1)
    first = adapt_function_tool(FakeFunctionTool("first", "First", schema(), invoke), binding)
    second = adapt_function_tool(FakeFunctionTool("second", "Second", schema(), invoke), binding)

    assert (await first.handler({"items": []}))["is_error"] is False
    exhausted = await second.handler({"items": []})
    assert exhausted["is_error"] is True
    assert exhausted["content"][0]["text"] == "agent tool-call limit exhausted"


@pytest.mark.asyncio
async def test_wait_for_agents_parks_and_balances_inference_slot() -> None:
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()  # Runner's surrounding active-inference lease.

    async def wait_for_child(_context: Any, _raw: str) -> str:
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
        semaphore.release()
        return "child completed"

    binding = ToolContextBinding("root", {"agent_id": "root"}, parking_semaphore=semaphore)
    adapted = adapt_function_tool(
        FakeFunctionTool("wait_for_agents", "Wait", schema(), wait_for_child), binding
    )

    result = await adapted.handler({"items": []})

    assert result["is_error"] is False
    assert semaphore.locked() is True
    semaphore.release()


@pytest.mark.asyncio
async def test_turn_input_provider_advances_host_controlled_context() -> None:
    history = ["first"]
    captured: list[list[Any]] = []

    async def invoke(context: Any, _raw: str) -> str:
        captured.append(context.turn_input)
        return "ok"

    adapted = adapt_function_tool(
        FakeFunctionTool("current", "Current", schema(), invoke),
        ToolContextBinding(
            "root",
            {"agent_id": "root"},
            turn_input=("stale",),
            turn_input_provider=lambda: tuple(history),
        ),
    )
    await adapted.handler({"items": []})
    history.append("second")
    await adapted.handler({"items": []})

    assert captured == [["first"], ["first", "second"]]
