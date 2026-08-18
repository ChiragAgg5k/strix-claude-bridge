"""Per-agent execution loop for the multi-agent bridge runtime."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from strix_claude_bridge import __version__
from strix_claude_bridge.backend import BackendEvent, SessionCheckpoint, tool_schema_digest
from strix_claude_bridge.runtime_state import cwd_identity
from strix_claude_bridge.single_agent import SingleAgentScanError, _record_subscription_usage
from strix_claude_bridge.strix_integration import (
    SUPPORTED_CLAUDE_SDK_VERSION,
    StrixAgentBridgeInput,
    build_claude_session_spec,
)

from .state import MultiAgentRunArtifacts

if TYPE_CHECKING:  # pragma: no cover
    from .runner import MultiAgentScanConfig


class _SessionInterruptHandle:
    def __init__(self, session: Any) -> None:
        self.session = session
        self.tasks: set[asyncio.Task[Any]] = set()
        self._active_wait_calls: set[str] = set()
        self._mailbox_wait_interrupt_pending = False

    def observe(self, event: BackendEvent) -> None:
        call_id = event.payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        if event.kind == "tool_call":
            name = str(event.payload.get("name") or "").rsplit("__", 1)[-1]
            if name == "wait_for_agents":
                self._active_wait_calls.add(call_id)
        elif event.kind == "tool_result":
            self._active_wait_calls.discard(call_id)

    def cancel(self, *, mode: str = "immediate") -> None:
        # Mailbox delivery only interrupts a coordinator wait. Interrupting arbitrary
        # shell/patch/network work would make its side effects indeterminate.
        if (
            mode not in {"immediate", "after_turn"}
            or not self._active_wait_calls
            or self._mailbox_wait_interrupt_pending
        ):
            return
        self._mailbox_wait_interrupt_pending = True
        pending = asyncio.create_task(self.session.interrupt())
        self.tasks.add(pending)
        pending.add_done_callback(self.tasks.discard)

    def consume_mailbox_wait_interrupt(self) -> bool:
        pending = self._mailbox_wait_interrupt_pending
        self._mailbox_wait_interrupt_pending = False
        return pending


@dataclass
class AgentRuntime:
    agent_id: str
    name: str
    parent_id: str | None
    task_text: str
    skills: list[str]
    parent_history: tuple[Any, ...]
    task: asyncio.Task[None] | None = None
    session: Any | None = None
    terminal_notice_sent: bool = False
    turns_used: int = 0
    cleanup_error: BaseException | None = None
    tools: list[Any] = field(default_factory=list, repr=False)


def _effective_agent_and_tools(
    *,
    sandbox_session: Any,
    scan_config: Mapping[str, Any],
    name: str,
    skills: list[str],
    is_root: bool,
) -> tuple[str, list[Any]]:
    from strix.agents.factory import build_strix_agent
    from strix.core.inputs import build_scope_context

    targets = list(scan_config.get("targets") or [])
    agent = build_strix_agent(
        name=name,
        skills=skills,
        is_root=is_root,
        scan_mode=str(scan_config.get("scan_mode") or "deep"),
        is_whitebox=any(item.get("type") == "local_code" for item in targets),
        interactive=False,
        chat_completions_tools=True,
        system_prompt_context=build_scope_context(dict(scan_config)),
    )
    prompt = str(agent.instructions or "")
    if not prompt.strip():
        raise SingleAgentScanError("Strix rendered an empty multi-agent prompt")
    tools = list(agent.tools)
    for capability in agent.capabilities:
        bound = capability.clone()
        bound.bind(sandbox_session)
        for tool in bound.tools():
            if getattr(tool, "name", None) == "apply_patch":
                tool.needs_approval = False
            tools.append(tool)
    names = [str(getattr(tool, "name", "")) for tool in tools]
    if any(not item for item in names) or len(names) != len(set(names)):
        raise SingleAgentScanError("Strix produced invalid multi-agent tools")
    lifecycle = "finish_scan" if is_root else "agent_finish"
    required = {"create_agent", "send_message_to_agent", "wait_for_agents", lifecycle}
    missing = sorted(required - set(names))
    if missing:
        raise SingleAgentScanError("Strix multi-agent tool set is missing: " + ", ".join(missing))
    return prompt, tools


class MultiAgentAgentExecutor:
    def __init__(
        self,
        *,
        config: MultiAgentScanConfig,
        backend: Any,
        semaphore: asyncio.Semaphore,
        coordinator: Any,
        bundle: Mapping[str, Any],
        artifacts: MultiAgentRunArtifacts,
        runtimes: Mapping[str, AgentRuntime],
        spawn_child: Callable[..., Awaitable[dict[str, Any]]],
    ) -> None:
        self.config = config
        self.backend = backend
        self.semaphore = semaphore
        self.coordinator = coordinator
        self.bundle = bundle
        self.artifacts = artifacts
        self.runtimes = runtimes
        self.spawn_child = spawn_child

    def _agent_context(self, runtime: AgentRuntime) -> dict[str, Any]:
        from strix.config import load_settings

        return {
            "coordinator": self.coordinator,
            "sandbox_session": self.bundle["session"],
            "caido_client": self.bundle["caido_client"],
            "agent_id": runtime.agent_id,
            "parent_id": runtime.parent_id,
            "task": runtime.task_text,
            "interactive": False,
            "spawn_child_agent": self.spawn_child,
            "max_context_images": load_settings().runtime.max_context_images,
        }

    async def _mailbox_input(self, agent_id: str) -> tuple[str | None, int]:
        count, items = await self.coordinator.consume_pending(agent_id, include_items=True)
        if count <= 0:
            return None, 0
        parts = [
            str(item.get("content", ""))
            for item in items
            if isinstance(item, dict) and str(item.get("content", "")).strip()
        ]
        return ("\n\n".join(parts) if parts else None), count

    def _checkpoint(self, runtime: AgentRuntime, spec: Any, result: Any) -> SessionCheckpoint:
        schemas = {
            str(getattr(tool, "name", "")): dict(getattr(tool, "params_json_schema", {}))
            for tool in runtime.tools
        }
        return SessionCheckpoint(
            provider_backend="claude-agent-sdk",
            backend_version=__version__,
            sdk_version=SUPPORTED_CLAUDE_SDK_VERSION,
            model=self.config.model,
            tool_schema_digest=tool_schema_digest(schemas),
            provider_session_id=str(result.provider_session_id),
            cwd_identity=cwd_identity(spec.cwd),
            last_settled_turn=runtime.turns_used,
            cli_version=f"bundled-with-sdk-{SUPPORTED_CLAUDE_SDK_VERSION}",
            settled=True,
            journal_clean=self.artifacts.journal.is_clean,
        )

    async def run(self, runtime: AgentRuntime, *, resume: bool = False) -> None:
        from strix.core.execution import notify_parent_on_terminal

        status = "failed"
        try:
            prompt, tools = _effective_agent_and_tools(
                sandbox_session=self.bundle["session"],
                scan_config=self.config.scan_config,
                name=runtime.name,
                skills=runtime.skills,
                is_root=runtime.parent_id is None,
            )
            runtime.tools = tools
            cwd = self.artifacts.bridge_state_dir / f"claude-{runtime.agent_id}"
            cwd.mkdir(parents=True, exist_ok=True)
            checkpoint = self.artifacts.checkpoint_for(runtime.agent_id) if resume else None
            current_history: list[Any] = list(runtime.parent_history or (runtime.task_text,))
            remaining_turns = self.config.max_turns - runtime.turns_used
            if remaining_turns < 1:
                raise SingleAgentScanError(
                    f"agent {runtime.agent_id} exhausted its persisted max-turn limit"
                )
            spec = build_claude_session_spec(
                StrixAgentBridgeInput(
                    agent_id=runtime.agent_id,
                    system_prompt=prompt,
                    cwd=cwd,
                    context=self._agent_context(runtime),
                    function_tools=tools,
                    turn_input=current_history,
                    turn_input_provider=lambda: tuple(current_history),
                    model=self.config.model,
                    max_turns=remaining_turns,
                    turn_timeout_s=self.config.turn_timeout_s,
                    resume_checkpoint=checkpoint,
                    journal=self.artifacts.journal,
                    max_tool_calls=self.config.max_tool_calls_per_agent,
                    parking_semaphore=self.semaphore,
                )
            )
            session = self.backend.create_session(spec)
            runtime.session = session
            handle = _SessionInterruptHandle(session)
            await self.artifacts.seed_agent(runtime.agent_id, runtime.task_text)
            viewer_session = self.artifacts.viewer_session_for(runtime.agent_id)
            await self.coordinator.attach_stream(runtime.agent_id, handle)
            await self.coordinator.attach_runtime(
                runtime.agent_id,
                session=viewer_session,
                interrupt_on_message=True,
            )
            initial = (
                "Resume the authorized task from the last settled Claude session. Check current "
                "agent messages and do not repeat completed destructive actions."
                if resume
                else runtime.task_text
            )
            recovery = 0
            first = True
            while True:
                async with self.semaphore:
                    if first:
                        await session.start(initial)
                        first = False
                    async for event in session.events():
                        handle.observe(event)
                        await self.artifacts.emit(event)
                        if event.kind == "rate_limit" and event.payload.get("status") == "rejected":
                            raise SingleAgentScanError(
                                "Claude subscription rate or plan limit reached; retry after "
                                "capacity resets or lower --max-concurrent-agents"
                            )
                result = session.result
                if result is None:
                    raise SingleAgentScanError("Claude agent ended without a terminal result")
                self.artifacts.record_usage(
                    result.usage,
                    model=self.config.model,
                    observed_models=result.models,
                )
                _record_subscription_usage(
                    self.artifacts.report_state,
                    agent_id=runtime.agent_id,
                    agent_name=runtime.name,
                    result=result,
                    model=self.config.model,
                )
                runtime.turns_used += int(result.turns or 0)
                if result.provider_session_id:
                    checkpoint = self._checkpoint(runtime, spec, result)
                    self.artifacts.record_agent(
                        agent_id=runtime.agent_id,
                        name=runtime.name,
                        parent_id=runtime.parent_id,
                        task=runtime.task_text,
                        skills=runtime.skills,
                        status=str(self.coordinator.statuses.get(runtime.agent_id, "running")),
                        checkpoint=checkpoint,
                    )
                current_status = str(self.coordinator.statuses.get(runtime.agent_id, "running"))
                if runtime.parent_id is None and self.artifacts.report_state.scan_results:
                    status = "completed"
                    await self.coordinator.set_status(runtime.agent_id, status)
                    return
                if runtime.parent_id is not None and current_status == "completed":
                    status = "completed"
                    return
                if current_status == "stopped":
                    status = "stopped"
                    return
                mailbox_text, _count = await self._mailbox_input(runtime.agent_id)
                mailbox_wait_interrupted = handle.consume_mailbox_wait_interrupt()
                if result.is_error:
                    reason = result.terminal_reason.casefold()
                    if any(word in reason for word in ("rate", "limit", "quota", "capacity")):
                        raise SingleAgentScanError(
                            "Claude subscription rate or plan limit reached; lower concurrency or "
                            "resume after capacity resets"
                        )
                    # The SDK reports `aborted_tools` when a proven coordinator mailbox
                    # interrupt settles an active wait tool. Recover only that exact in-process
                    # case, and only when this agent has no indeterminate side effect.
                    recoverable_wait = (
                        reason == "aborted_tools"
                        and mailbox_wait_interrupted
                        and self.artifacts.journal.is_agent_clean(runtime.agent_id)
                    )
                    if not recoverable_wait:
                        raise SingleAgentScanError(
                            f"Claude agent {runtime.agent_id} failed ({result.terminal_reason})"
                        )
                if mailbox_text:
                    current_history.append({"role": "user", "content": mailbox_text})
                    async with self.semaphore:
                        await session.continue_with(mailbox_text)
                    recovery = 0
                    continue
                if runtime.parent_id is None and any(
                    item.parent_id == runtime.agent_id and item.task and not item.task.done()
                    for item in self.runtimes.values()
                ):
                    await self.coordinator.wait_for_message(runtime.agent_id, timeout=1.0)
                    mailbox_text, _count = await self._mailbox_input(runtime.agent_id)
                    if mailbox_text:
                        current_history.append({"role": "user", "content": mailbox_text})
                        async with self.semaphore:
                            await session.continue_with(mailbox_text)
                        continue
                recovery += 1
                if recovery > self.config.recovery_turns:
                    status = "stopped"
                    await self.coordinator.set_status(runtime.agent_id, status)
                    raise SingleAgentScanError(
                        f"agent {runtime.agent_id} ended without its lifecycle tool"
                    )
                nudge = (
                    "Continue the authorized scan, account for all child outcomes, then call "
                    "finish_scan exactly once."
                    if runtime.parent_id is None
                    else "Complete the assigned subtask and call agent_finish exactly once."
                )
                current_history.append({"role": "user", "content": nudge})
                await self.artifacts.append_user_text(runtime.agent_id, nudge)
                async with self.semaphore:
                    await session.continue_with(nudge)
        except asyncio.CancelledError:
            status = "interrupted"
            if runtime.session is not None:
                try:
                    await runtime.session.cancel()
                except BaseException as exc:
                    runtime.cleanup_error = exc
            await self.coordinator.set_status(runtime.agent_id, "stopped")
            raise
        except BaseException as exc:
            await self.coordinator.set_status(runtime.agent_id, "failed", error=type(exc).__name__)
            if runtime.parent_id is None:
                raise
        finally:
            active_exception = sys.exc_info()[0] is not None
            close_error = runtime.cleanup_error
            if runtime.session is not None:
                try:
                    await runtime.session.close()
                except BaseException as exc:
                    if close_error is None:
                        close_error = exc
            if close_error is not None:
                status = "failed"
                await self.coordinator.set_status(
                    runtime.agent_id, "failed", error="SDKCleanupError"
                )
                await self.artifacts.emit(
                    BackendEvent(
                        "sdk_cleanup_failed",
                        runtime.agent_id,
                        {"error_type": type(close_error).__name__},
                    )
                )
            if runtime.parent_id is not None:
                await notify_parent_on_terminal(self.coordinator, runtime.agent_id, status)
            self.artifacts.record_agent(
                agent_id=runtime.agent_id,
                name=runtime.name,
                parent_id=runtime.parent_id,
                task=runtime.task_text,
                skills=runtime.skills,
                status=status,
                checkpoint=self.artifacts.checkpoint_for(runtime.agent_id),
            )
            if close_error is not None and not active_exception and runtime.parent_id is None:
                raise close_error
