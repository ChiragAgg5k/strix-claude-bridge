from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

from strix_claude_bridge.events import JsonlEventWriter
from strix_claude_bridge.probe import ProbeCancelled, ProbeTerminalError, run_live_probe
from strix_claude_bridge.sandbox import ExecResult


class FakeExecutor:
    instances: ClassVar[list[FakeExecutor]] = []

    def __init__(self, image: str, *, timeout_s: float) -> None:
        self.image = image
        self.timeout_s = timeout_s
        self.closed = False
        self.__class__.instances.append(self)

    async def __aenter__(self) -> FakeExecutor:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def exec(self, _command: str) -> ExecResult:
        return ExecResult(0, "ok", "")

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeResultMessage:
    session_id: str = "test-session"
    usage: dict[str, int] | None = None
    is_error: bool = False
    terminal_reason: str | None = "completed"
    api_error_status: int | None = None


class CompletingClient:
    options: Any = None
    instance: CompletingClient | None = None

    def __init__(self, options: Any) -> None:
        self.__class__.options = options
        self.connected = False
        self.disconnected = False
        self.__class__.instance = self

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnected = True

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):
        yield FakeResultMessage(usage={"input_tokens": 1})

    async def interrupt(self) -> None:
        raise AssertionError("normal completion must not interrupt")


class InterruptibleClient(CompletingClient):
    instance: InterruptibleClient | None = None

    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.interrupted = asyncio.Event()
        self.interrupted_while_connected = False
        self.__class__.instance = self

    async def receive_response(self):
        await self.interrupted.wait()
        if False:
            yield None

    async def interrupt(self) -> None:
        self.interrupted_while_connected = self.connected
        self.interrupted.set()


class ErrorClient(CompletingClient):
    result = FakeResultMessage(is_error=True, terminal_reason="max_turns", api_error_status=429)

    async def receive_response(self):
        yield self.result


@pytest.mark.asyncio
async def test_normal_probe_streams_result_and_cleans_up() -> None:
    FakeExecutor.instances.clear()
    output = io.StringIO()

    await run_live_probe(
        prompt="safe prompt",
        image="alpine:3.21",
        writer=JsonlEventWriter(output),
        client_factory=CompletingClient,
        executor_factory=FakeExecutor,
    )

    raw_output = output.getvalue()
    events = [json.loads(line) for line in raw_output.splitlines()]
    assert "sdk_message" in [event["kind"] for event in events]
    assert "test-session" not in raw_output
    sdk_message = next(event for event in events if event["kind"] == "sdk_message")
    assert "session_id" not in sdk_message
    assert "session_id" not in sdk_message["payload"]
    assert events[-1]["kind"] == "sandbox_closed"
    assert FakeExecutor.instances[-1].closed
    assert CompletingClient.instance is not None
    assert CompletingClient.instance.disconnected
    options = CompletingClient.options
    assert options.allowed_tools == ["mcp__strix_spike__sandbox_exec"]
    assert options.tools == []
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.skills == []
    assert isinstance(options.cwd, Path)
    assert options.cwd.name.startswith("strix-claude-sdk-")
    assert not options.cwd.exists()


@pytest.mark.asyncio
async def test_programmatic_cancellation_interrupts_and_cleans_up() -> None:
    FakeExecutor.instances.clear()
    cancellation = asyncio.Event()
    output = io.StringIO()
    task = asyncio.create_task(
        run_live_probe(
            prompt="safe prompt",
            image="alpine:3.21",
            writer=JsonlEventWriter(output),
            cancellation_event=cancellation,
            client_factory=InterruptibleClient,
            executor_factory=FakeExecutor,
            interrupt_grace_s=0.5,
        )
    )
    for _ in range(100):
        if InterruptibleClient.instance is not None:
            break
        await asyncio.sleep(0.001)
    cancellation.set()

    with pytest.raises(ProbeCancelled):
        await task

    assert InterruptibleClient.instance is not None
    assert InterruptibleClient.instance.interrupted.is_set()
    assert InterruptibleClient.instance.interrupted_while_connected
    assert FakeExecutor.instances[-1].closed
    kinds = [json.loads(line)["kind"] for line in output.getvalue().splitlines()]
    assert "cancellation_requested" in kinds
    assert "cancelled" in kinds
    assert kinds[-1] == "sandbox_closed"


@pytest.mark.asyncio
async def test_task_cancellation_interrupts_before_disconnect() -> None:
    output = io.StringIO()
    task = asyncio.create_task(
        run_live_probe(
            prompt="safe prompt",
            image="alpine:3.21",
            writer=JsonlEventWriter(output),
            client_factory=InterruptibleClient,
            executor_factory=FakeExecutor,
            interrupt_grace_s=0.5,
        )
    )
    for _ in range(100):
        instance = InterruptibleClient.instance
        if instance is not None and instance.connected:
            break
        await asyncio.sleep(0.001)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert InterruptibleClient.instance is not None
    assert InterruptibleClient.instance.interrupted_while_connected
    assert InterruptibleClient.instance.disconnected
    kinds = [json.loads(line)["kind"] for line in output.getvalue().splitlines()]
    assert kinds.index("cancellation_requested") < kinds.index("sdk_disconnected")
    assert kinds[-1] == "sandbox_closed"


@pytest.mark.asyncio
async def test_terminal_sdk_error_is_reported_and_raised() -> None:
    output = io.StringIO()

    with pytest.raises(ProbeTerminalError, match="max_turns"):
        await run_live_probe(
            prompt="safe prompt",
            image="alpine:3.21",
            writer=JsonlEventWriter(output),
            client_factory=ErrorClient,
            executor_factory=FakeExecutor,
        )

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    failure = next(event for event in events if event["kind"] == "terminal_failure")
    assert failure["payload"]["terminal_reason"] == "max_turns"
    assert failure["payload"]["api_error_status"] == 429
    assert "completed" not in [event["kind"] for event in events]
