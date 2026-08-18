"""Reusable Claude Agent SDK execution backend."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from enum import Enum
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import HookMatcher

from strix_claude_bridge.auth import require_subscription_environment
from strix_claude_bridge.backend import (
    AgentSessionSpec,
    BackendEvent,
    BackendResult,
    BackendStateError,
    ExecutionBackend,
    ExecutionSession,
)
from strix_claude_bridge.event_adapter import normalize_sdk_message, result_from_sdk_message


class SessionState(str, Enum):
    NEW = "new"
    RUNNING = "running"
    IDLE = "idle"
    INTERRUPTING = "interrupting"
    FAILED = "failed"
    CLOSED = "closed"


_DONE = object()
ClientFactory = Callable[[ClaudeAgentOptions], Any]


def _completed_lifecycle_response(value: Any) -> bool:
    """Recognize a successful Strix root or child lifecycle result."""
    if isinstance(value, str):
        try:
            return _completed_lifecycle_response(json.loads(value))
        except (TypeError, ValueError):
            return False
    if isinstance(value, Mapping):
        if value.get("success") is True and (
            value.get("scan_completed") is True or value.get("agent_completed") is True
        ):
            return True
        return any(
            _completed_lifecycle_response(value.get(key))
            for key in ("content", "text", "tool_response")
            if key in value
        )
    if isinstance(value, list | tuple):
        return any(_completed_lifecycle_response(item) for item in value)
    text = getattr(value, "text", None)
    return _completed_lifecycle_response(text) if isinstance(text, str) else False


class ClaudeAgentSDKSession(ExecutionSession):
    """Own exactly one connected SDK client and one message consumer."""

    def __init__(
        self,
        spec: AgentSessionSpec,
        *,
        client_factory: ClientFactory = ClaudeSDKClient,
        interrupt_grace_s: float = 5.0,
        enforce_subscription_environment: bool = True,
    ) -> None:
        self.spec = spec
        self._client_factory = client_factory
        self._interrupt_grace_s = interrupt_grace_s
        self._enforce_subscription_environment = enforce_subscription_environment
        self._client: Any = None
        self._state = SessionState.NEW
        self._pump_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[BackendEvent | object] | None = None
        self._events_claimed = False
        self._events_consumed = True
        self._result: BackendResult | None = None
        self._sequence = 0
        self._turns_used = 0
        self._lifecycle_completed = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def result(self) -> BackendResult | None:
        return self._result

    async def _post_tool_use_hook(
        self, hook_input: Any, _tool_use_id: str | None, _context: Any
    ) -> dict[str, Any]:
        tool_name = getattr(hook_input, "tool_name", None)
        response = getattr(hook_input, "tool_response", None)
        if isinstance(hook_input, Mapping):
            tool_name = hook_input.get("tool_name", tool_name)
            response = hook_input.get("tool_response", response)
        if tool_name in self.spec.terminal_tools and _completed_lifecycle_response(response):
            self._lifecycle_completed = True
            return {
                "continue_": False,
                "stopReason": "Strix lifecycle tool completed successfully",
            }
        return {}

    def _options(
        self,
        *,
        max_turns: int | None = None,
        resume_session_id: str | None = None,
    ) -> ClaudeAgentOptions:
        hooks = None
        if self.spec.terminal_tools:
            matcher = "|".join(self.spec.terminal_tools)
            hooks = {
                "PostToolUse": [HookMatcher(matcher=matcher, hooks=[self._post_tool_use_hook])]
            }
        return ClaudeAgentOptions(
            system_prompt=self.spec.system_prompt,
            tools=[],
            allowed_tools=list(self.spec.allowed_tools),
            mcp_servers={
                name: dict(server.configuration) for name, server in self.spec.mcp_servers.items()
            },
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            cwd=self.spec.cwd,
            permission_mode="dontAsk",
            include_partial_messages=True,
            max_turns=self.spec.max_turns if max_turns is None else max_turns,
            model=self.spec.model,
            resume=(
                resume_session_id
                if resume_session_id is not None
                else (
                    self.spec.resume_checkpoint.provider_session_id
                    if self.spec.resume_checkpoint is not None
                    else None
                )
            ),
            fork_session=False,
            hooks=hooks,
        )

    async def start(self, initial_input: str) -> None:
        if not isinstance(initial_input, str) or not initial_input.strip():
            raise ValueError("initial_input must not be empty")
        async with self._lifecycle_lock:
            if self._state is not SessionState.NEW:
                raise BackendStateError(f"cannot start session in state {self._state.value}")
            if not self.spec.cwd.is_dir():
                raise ValueError("session cwd must be an existing directory")
            if self._enforce_subscription_environment:
                require_subscription_environment()
            self._client = self._client_factory(self._options())
            try:
                await self._client.connect()
                await self._begin_turn(initial_input)
            except BaseException:
                self._state = SessionState.FAILED
                if self._client is not None:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                raise

    async def _begin_turn(self, input_text: str) -> None:
        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must not be empty")
        assert self._client is not None
        await self._client.query(input_text)
        self._queue = asyncio.Queue()
        self._lifecycle_completed = False
        self._events_claimed = False
        self._events_consumed = False
        self._result = None
        self._state = SessionState.RUNNING
        self._pump_task = asyncio.create_task(self._pump_response())

    async def _consume_response(self) -> None:
        assert self._client is not None
        async for message in self._client.receive_response():
            is_terminal = type(message).__name__ == "ResultMessage"
            if is_terminal:
                normalized_result = result_from_sdk_message(message)
                self._result = (
                    BackendResult(
                        normalized_result.provider_session_id,
                        "completed",
                        False,
                        normalized_result.usage,
                        normalized_result.turns,
                        normalized_result.models,
                    )
                    if self._lifecycle_completed
                    else normalized_result
                )
                if self._result.turns is not None:
                    self._turns_used += self._result.turns
                    if self.spec.max_turns is not None and self._turns_used > self.spec.max_turns:
                        self._result = BackendResult(
                            self._result.provider_session_id,
                            "turn_limit_exceeded",
                            True,
                            self._result.usage,
                            self._result.turns,
                            self._result.models,
                        )
            for event in normalize_sdk_message(message, agent_id=self.spec.agent_id):
                if is_terminal and self._lifecycle_completed:
                    event = replace(
                        event,
                        payload={
                            "terminal_reason": "completed",
                            "is_error": False,
                            "turns": self._result.turns if self._result else None,
                            "usage": dict(self._result.usage) if self._result else {},
                            "lifecycle_tool": True,
                        },
                    )
                self._sequence += 1
                assert self._queue is not None
                await self._queue.put(replace(event, sequence=self._sequence))

    async def _emit_provider_error(self, category: str, **payload: Any) -> None:
        assert self._queue is not None
        self._sequence += 1
        await self._queue.put(
            BackendEvent(
                "provider_error",
                self.spec.agent_id,
                {"category": category, **payload},
                sequence=self._sequence,
            )
        )

    async def _disconnect_after_failed_drain(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:
            pass
        finally:
            self._client = None

    async def _pump_response(self) -> None:
        assert self._queue is not None
        receiver = asyncio.create_task(self._consume_response())
        timed_out = False
        safely_drained = True
        try:
            if self.spec.turn_timeout_s is None:
                await receiver
            else:
                done, _ = await asyncio.wait({receiver}, timeout=self.spec.turn_timeout_s)
                if done:
                    await receiver
                else:
                    timed_out = True
                    self._state = SessionState.INTERRUPTING
                    try:
                        await self._interrupt_client()
                    except Exception as exc:
                        await self._emit_provider_error(
                            "timeout_interrupt_failed", error_type=type(exc).__name__
                        )
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(receiver), timeout=self._interrupt_grace_s
                        )
                    except TimeoutError:
                        safely_drained = False
                        receiver.cancel()
                        await asyncio.gather(receiver, return_exceptions=True)
                        await self._disconnect_after_failed_drain()
            if timed_out:
                terminal = self._result
                self._result = BackendResult(
                    terminal.provider_session_id if terminal else None,
                    "timeout",
                    True,
                    terminal.usage if terminal else {},
                    terminal.turns if terminal else None,
                    terminal.models if terminal else (),
                )
                await self._emit_provider_error("timeout", timeout_seconds=self.spec.turn_timeout_s)
                self._state = SessionState.IDLE if safely_drained else SessionState.FAILED
            elif self._result is None:
                self._result = BackendResult(None, "stream_ended", True)
                await self._emit_provider_error("stream_ended_without_terminal_result")
                self._state = SessionState.FAILED
            else:
                self._state = SessionState.IDLE
        except asyncio.CancelledError:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            raise
        except Exception as exc:
            self._result = BackendResult(None, "provider_error", True)
            await self._emit_provider_error(type(exc).__name__)
            self._state = SessionState.FAILED
        finally:
            await self._queue.put(_DONE)

    async def _event_iterator(self) -> AsyncIterator[BackendEvent]:
        if self._queue is None:
            raise BackendStateError("session has no active turn")
        if self._events_claimed:
            raise BackendStateError("events for this turn already have a consumer")
        self._events_claimed = True
        try:
            while True:
                item = await self._queue.get()
                if item is _DONE:
                    break
                assert isinstance(item, BackendEvent)
                yield item
        finally:
            self._events_consumed = True

    def events(self) -> AsyncIterator[BackendEvent]:
        return self._event_iterator()

    async def continue_with(self, input_text: str) -> None:
        async with self._lifecycle_lock:
            if self._state is not SessionState.IDLE or not self._events_consumed:
                raise BackendStateError("prior turn must settle and its events must be consumed")
            remaining = (
                None if self.spec.max_turns is None else self.spec.max_turns - self._turns_used
            )
            if remaining is not None and remaining < 1:
                raise BackendStateError("cumulative provider turn limit is exhausted")
            provider_session_id = self._result.provider_session_id if self._result else None
            if not provider_session_id:
                raise BackendStateError(
                    "settled provider session identifier is required for continuation"
                )
            # ClaudeAgentOptions.max_turns is fixed when a transport is created.
            # Reconnect to the same native session with only the remaining host
            # allowance so an 8-turn query cannot receive another 10-turn budget.
            assert self._client is not None
            try:
                await self._client.disconnect()
                self._client = self._client_factory(
                    self._options(
                        max_turns=remaining,
                        resume_session_id=provider_session_id,
                    )
                )
                await self._client.connect()
            except BaseException:
                self._state = SessionState.FAILED
                raise
            await self._begin_turn(input_text)

    async def inject(self, input_text: str) -> None:
        await self.continue_with(input_text)

    async def _interrupt_client(self) -> None:
        if self._client is None:
            return
        try:
            await asyncio.wait_for(self._client.interrupt(), timeout=self._interrupt_grace_s)
        except TimeoutError:
            # The owning session still proceeds to bounded task cleanup and SDK disconnect.
            pass

    async def interrupt(self) -> None:
        async with self._lifecycle_lock:
            if self._state not in {SessionState.RUNNING, SessionState.INTERRUPTING}:
                return
            self._state = SessionState.INTERRUPTING
            await self._interrupt_client()

    async def _settle_pump(self) -> None:
        task = self._pump_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._interrupt_grace_s)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def cancel(self) -> None:
        was_active = self._state in {SessionState.RUNNING, SessionState.INTERRUPTING}
        try:
            await self.close()
        finally:
            if was_active:
                self._result = BackendResult(None, "cancelled", True)

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._state is SessionState.CLOSED:
                return
            first_error: BaseException | None = None
            try:
                if self._state in {SessionState.RUNNING, SessionState.INTERRUPTING}:
                    self._state = SessionState.INTERRUPTING
                    try:
                        await self._interrupt_client()
                    except BaseException as exc:
                        first_error = exc
                    try:
                        await self._settle_pump()
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
            finally:
                try:
                    if self._client is not None:
                        # The SDK owns graceful wait/terminate/kill escalation.
                        await self._client.disconnect()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                finally:
                    self._client = None
                    self._state = SessionState.CLOSED
            if first_error is not None:
                raise first_error


class ClaudeAgentSDKBackend(ExecutionBackend):
    """Execution backend selected explicitly by Strix configuration."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory = ClaudeSDKClient,
        interrupt_grace_s: float = 5.0,
        enforce_subscription_environment: bool = True,
    ) -> None:
        self._client_factory = client_factory
        self._interrupt_grace_s = interrupt_grace_s
        self._enforce_subscription_environment = enforce_subscription_environment

    def create_session(self, spec: AgentSessionSpec) -> ClaudeAgentSDKSession:
        return ClaudeAgentSDKSession(
            spec,
            client_factory=self._client_factory,
            interrupt_grace_s=self._interrupt_grace_s,
            enforce_subscription_environment=self._enforce_subscription_environment,
        )
