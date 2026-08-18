"""Official Claude Agent SDK lifecycle and in-process MCP compatibility probe."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool

from strix_claude_bridge.events import JsonlEventWriter, to_secret_safe
from strix_claude_bridge.sandbox import DockerSandboxExecutor

_SYSTEM_PROMPT = """You are running an authorized, non-destructive compatibility probe.
The only available action is sandbox_exec. It executes in a disposable, network-disabled Docker
container, never on the host. Use it only when the user explicitly asks, then report the result.
Do not request credentials, inspect authentication files, or attempt to escape the container.
"""


class ProbeCancelled(RuntimeError):
    """Programmatic cancellation completed after cleanup."""


class ProbeTerminalError(RuntimeError):
    """The SDK returned a terminal result that did not complete successfully."""


def _text_bytes(value: str) -> int:
    return len(value.encode("utf-8", errors="replace"))


def make_sandbox_tool(
    executor: DockerSandboxExecutor,
    writer: JsonlEventWriter,
    *,
    include_sensitive_content: bool = False,
) -> Any:
    """Create the SDK tool definition around the isolated executor.

    Commands and output are sent to the SDK as required for the MCP interaction, but JSONL is
    metadata-only by default. Raw content requires an explicit local diagnostic opt-in.
    """

    @tool(
        "sandbox_exec",
        "Run one shell command inside the configured disposable Docker container only.",
        {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute inside the disposable container.",
                    "minLength": 1,
                    "maxLength": 16384,
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )
    async def sandbox_exec(args: dict[str, Any]) -> dict[str, Any]:
        command = args.get("command")
        call_payload = {
            "tool": "sandbox_exec",
            "command_bytes": _text_bytes(command) if isinstance(command, str) else None,
        }
        if include_sensitive_content:
            call_payload["command"] = command
        await writer.emit("tool_call", call_payload)
        try:
            result = await executor.exec(command)
            payload = {
                "exit_code": result.exit_code,
                "stdout_bytes": _text_bytes(result.stdout),
                "stderr_bytes": _text_bytes(result.stderr),
                "truncated": result.truncated,
            }
            if include_sensitive_content:
                payload.update({"stdout": result.stdout, "stderr": result.stderr})
            await writer.emit("tool_result", payload, tool="sandbox_exec")
            text = (
                f"exit_code={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            if result.truncated:
                text += "\n[output truncated]"
            return {
                "content": [{"type": "text", "text": text}],
                "is_error": result.exit_code != 0,
            }
        except asyncio.CancelledError:
            await writer.emit("tool_cancelled", {"tool": "sandbox_exec"})
            raise
        except Exception as exc:
            message = to_secret_safe(str(exc))
            error_payload = {"tool": "sandbox_exec", "error_type": type(exc).__name__}
            if include_sensitive_content:
                error_payload["message"] = message
            await writer.emit("tool_error", error_payload)
            return {
                "content": [{"type": "text", "text": f"sandbox_exec failed: {message}"}],
                "is_error": True,
            }

    return sandbox_exec


def build_mcp_server(
    executor: DockerSandboxExecutor,
    writer: JsonlEventWriter,
    *,
    include_sensitive_content: bool = False,
) -> Any:
    """Build the pinned SDK's in-process MCP server around the isolated executor."""
    return create_sdk_mcp_server(
        name="strix_spike",
        version="0.1.0",
        tools=[
            make_sandbox_tool(
                executor,
                writer,
                include_sensitive_content=include_sensitive_content,
            )
        ],
    )


async def _interrupt(client: Any, writer: JsonlEventWriter, grace_s: float) -> None:
    await writer.emit("cancellation_requested", {"grace_seconds": grace_s})
    try:
        await asyncio.wait_for(client.interrupt(), timeout=grace_s)
    except Exception as exc:
        await writer.emit("interrupt_error", {"error_type": type(exc).__name__})


async def _cancel_and_await(*tasks: asyncio.Task[Any] | None) -> None:
    active = [task for task in tasks if task is not None]
    for task in active:
        if not task.done():
            task.cancel()
    if active:
        await asyncio.gather(*active, return_exceptions=True)


def _message_metadata(message: Any) -> dict[str, Any]:
    """Return non-content SDK fields useful for compatibility evidence."""
    fields = (
        "usage",
        "model_usage",
        "is_error",
        "api_error_status",
        "terminal_reason",
        "stop_reason",
        "subtype",
        "num_turns",
        "duration_ms",
        "duration_api_ms",
    )
    return {
        name: getattr(message, name) for name in fields if getattr(message, name, None) is not None
    }


def _terminal_failure(message: Any) -> bool:
    if type(message).__name__ != "ResultMessage" and not hasattr(message, "is_error"):
        return False
    terminal_reason = getattr(message, "terminal_reason", None)
    return bool(
        getattr(message, "is_error", False)
        or getattr(message, "api_error_status", None) is not None
        or terminal_reason not in (None, "completed")
    )


async def run_live_probe(
    *,
    prompt: str,
    image: str,
    writer: JsonlEventWriter,
    cancellation_event: asyncio.Event | None = None,
    model: str | None = None,
    command_timeout_s: float = 30.0,
    max_turns: int = 4,
    interrupt_grace_s: float = 3.0,
    include_sensitive_content: bool = False,
    client_factory: Callable[[ClaudeAgentOptions], Any] = ClaudeSDKClient,
    executor_factory: Callable[..., DockerSandboxExecutor] = DockerSandboxExecutor,
) -> None:
    """Run one live turn with deterministic cancellation and resource cleanup."""
    cancellation_event = cancellation_event or asyncio.Event()
    executor = executor_factory(image, timeout_s=command_timeout_s)
    client: Any = None
    response_task: asyncio.Task[Any] | None = None
    cancellation_task: asyncio.Task[Any] | None = None
    try:
        async with executor:
            await writer.emit("sandbox_started", {"image": image})
            server = build_mcp_server(
                executor,
                writer,
                include_sensitive_content=include_sensitive_content,
            )
            with tempfile.TemporaryDirectory(prefix="strix-claude-sdk-") as sdk_cwd:
                options = ClaudeAgentOptions(
                    system_prompt=_SYSTEM_PROMPT,
                    tools=[],
                    allowed_tools=["mcp__strix_spike__sandbox_exec"],
                    mcp_servers={"strix_spike": server},
                    strict_mcp_config=True,
                    setting_sources=[],
                    skills=[],
                    cwd=Path(sdk_cwd),
                    permission_mode="dontAsk",
                    include_partial_messages=True,
                    max_turns=max_turns,
                    model=model,
                )
                client = client_factory(options)
                await client.connect()
                try:
                    await writer.emit(
                        "sdk_connected",
                        {"sdk": "claude-agent-sdk", "model": model or "sdk-default"},
                    )
                    await client.query(prompt)
                    terminal_message: Any = None

                    async def consume() -> None:
                        nonlocal terminal_message
                        async for message in client.receive_response():
                            if type(message).__name__ == "ResultMessage" or hasattr(
                                message, "is_error"
                            ):
                                terminal_message = message
                            payload = (
                                message if include_sensitive_content else _message_metadata(message)
                            )
                            await writer.emit(
                                "sdk_message",
                                payload,
                                message_type=type(message).__name__,
                            )

                    response_task = asyncio.create_task(consume())
                    cancellation_task = asyncio.create_task(cancellation_event.wait())
                    done, _ = await asyncio.wait(
                        {response_task, cancellation_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if cancellation_task in done and cancellation_event.is_set():
                        await _interrupt(client, writer, interrupt_grace_s)
                        try:
                            await asyncio.wait_for(response_task, timeout=interrupt_grace_s)
                        except TimeoutError:
                            await _cancel_and_await(response_task)
                        await _cancel_and_await(cancellation_task)
                        await writer.emit("cancelled", {"source": "programmatic"})
                        raise ProbeCancelled("probe cancelled")
                    await _cancel_and_await(cancellation_task)
                    await response_task
                    if terminal_message is not None and _terminal_failure(terminal_message):
                        metadata = _message_metadata(terminal_message)
                        await writer.emit("terminal_failure", metadata)
                        reason = (
                            metadata.get("terminal_reason") or metadata.get("subtype") or "error"
                        )
                        raise ProbeTerminalError(f"SDK probe terminated unsuccessfully: {reason}")
                    await writer.emit("completed", None)
                except asyncio.CancelledError:
                    # Task/SIGINT cancellation must interrupt while the client is still connected.
                    await asyncio.shield(_interrupt(client, writer, interrupt_grace_s))
                    await asyncio.shield(_cancel_and_await(response_task, cancellation_task))
                    await asyncio.shield(writer.emit("cancelled", {"source": "task_or_signal"}))
                    raise
                finally:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=interrupt_grace_s)
                    except Exception as exc:
                        await writer.emit("disconnect_error", {"error_type": type(exc).__name__})
                    await writer.emit("sdk_disconnected", None)
    finally:
        # Executor.close is idempotent and retries a prior failed removal.
        await asyncio.shield(executor.close())
        await writer.emit("sandbox_closed", {"image": image})
