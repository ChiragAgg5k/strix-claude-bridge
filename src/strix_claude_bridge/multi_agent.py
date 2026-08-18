"""Experimental multi-agent Strix coordinator backed by one Claude session per agent."""

from __future__ import annotations

import asyncio
import hashlib
import io
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strix_claude_bridge import __version__
from strix_claude_bridge.auth import require_subscription_environment
from strix_claude_bridge.backend import BackendEvent, SessionCheckpoint, tool_schema_digest
from strix_claude_bridge.claude_backend import ClaudeAgentSDKBackend
from strix_claude_bridge.runtime_state import (
    RunStateStore,
    ToolInvocationJournal,
    cwd_identity,
    secure_run_tree,
)
from strix_claude_bridge.single_agent import (
    BridgeEventSink,
    SingleAgentScanError,
    SingleAgentScanOutcome,
    _cleanup_strix_bundle,
    _create_strix_bundle,
    _deterministic_report_dedupe,
    _isolated_strix_tool_state,
    _record_subscription_usage,
)
from strix_claude_bridge.strix_integration import (
    SUPPORTED_CLAUDE_SDK_VERSION,
    StrixAgentBridgeInput,
    build_claude_session_spec,
    verify_runtime_compatibility,
)


@dataclass(frozen=True)
class MultiAgentScanConfig:
    scan_config: Mapping[str, Any]
    run_name: str
    image: str
    local_sources: Sequence[Mapping[str, Any]] = ()
    extra_files: Sequence[Mapping[str, Any]] = ()
    model: str | None = None
    max_turns: int = 100
    max_runtime_s: float = 3600
    max_concurrent_agents: int = 2
    max_agents: int = 8
    max_tool_calls_per_agent: int | None = None
    turn_timeout_s: float | None = None
    recovery_turns: int = 2
    cleanup_on_exit: bool = True
    resume_token: str | None = None

    def __post_init__(self) -> None:
        if self.resume_token is not None:
            raise ValueError(
                "process-restart resume is disabled until durable multi-agent graph and "
                "provider tool-use reconciliation are implemented"
            )
        if not self.run_name.strip() or not self.image.strip():
            raise ValueError("run name and sandbox image must not be empty")
        if self.max_turns < 1 or self.max_runtime_s <= 0:
            raise ValueError("max turns and max runtime must be positive")
        if self.max_concurrent_agents < 1 or self.max_agents < 1:
            raise ValueError("agent limits must be positive")
        if self.max_concurrent_agents > self.max_agents:
            raise ValueError("max concurrent agents cannot exceed max agents")


class _DurableCoordinatorMixin:
    """Snapshot mailbox audit metadata before requesting an SDK interruption."""

    async def snapshot(self) -> dict[str, Any]:
        data = await super().snapshot()  # type: ignore[misc]
        metadata = data.get("metadata", {})
        data["metadata"] = {}
        for agent_id, raw_metadata in metadata.items():
            sanitized = dict(raw_metadata)
            task = sanitized.pop("task", None)
            if task is not None:
                sanitized["task_sha256"] = hashlib.sha256(str(task).encode()).hexdigest()
            data["metadata"][agent_id] = sanitized
        mailboxes = data.get("mailboxes", {})
        data["mailboxes"] = {
            agent_id: [
                {key: message[key] for key in ("id", "from", "type", "priority") if key in message}
                | {
                    "content_sha256": hashlib.sha256(
                        str(message.get("content", "")).encode()
                    ).hexdigest()
                }
                for message in messages
            ]
            for agent_id, messages in mailboxes.items()
        }
        return data

    async def send(
        self, target_agent_id: str, message: dict[str, Any], *, interrupt: bool = True
    ) -> bool:
        delivered = await super().send(target_agent_id, message, interrupt=False)  # type: ignore[misc]
        if not delivered or not interrupt:
            return delivered
        async with self._lock:
            runtime = self.runtimes.get(target_agent_id)
            stream = runtime.stream if runtime is not None else None
            enabled = bool(runtime and runtime.interrupt_on_message)
        if stream is not None and enabled:
            stream.cancel(mode="immediate")
        return delivered


class _MailboxSessionMirror:
    """Minimal in-memory Strix Session sink; graph files are diagnostics only."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    async def add_items(self, items: list[Any]) -> None:
        self.items.extend(items)


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
class _AgentRuntime:
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


class MultiAgentScanRunner:
    def __init__(
        self,
        config: MultiAgentScanConfig,
        *,
        backend: Any | None,
        event_sink: BridgeEventSink | None,
        simulated_inference: bool,
    ) -> None:
        self.config = config
        self.backend = backend or ClaudeAgentSDKBackend()
        self.event_sink = event_sink
        self.simulated_inference = simulated_inference
        self.semaphore = asyncio.Semaphore(config.max_concurrent_agents)
        self.runtimes: dict[str, _AgentRuntime] = {}
        self.child_tasks: set[asyncio.Task[None]] = set()
        self.bundle: Mapping[str, Any] | None = None
        self.report_state: Any = None
        self.coordinator: Any = None
        self.store: RunStateStore | None = None
        self.journal: ToolInvocationJournal | None = None
        self.root_id = "root0001"
        self._agent_counter = 1
        self._shutting_down = False

    async def _emit(self, event: BackendEvent) -> None:
        assert self.store is not None
        self.store.append_event(event)
        if self.event_sink is not None:
            await self.event_sink(event)

    async def _spawn_child(
        self,
        *,
        parent_ctx: Mapping[str, Any],
        name: str,
        task: str,
        skills: list[str],
        parent_history: list[Any],
    ) -> dict[str, Any]:
        if self._shutting_down:
            raise SingleAgentScanError("scan is shutting down; no new child may start")
        if len(self.runtimes) >= self.config.max_agents:
            raise SingleAgentScanError("maximum agent count is exhausted")
        self._agent_counter += 1
        agent_id = f"agent{self._agent_counter:03d}"
        parent_id = str(parent_ctx["agent_id"])
        runtime = _AgentRuntime(
            agent_id,
            name,
            parent_id,
            task,
            list(skills),
            tuple(parent_history[-20:]),
        )
        self.runtimes[agent_id] = runtime
        await self.coordinator.register(agent_id, name, parent_id, task=task, skills=list(skills))
        assert self.store is not None
        self.store.record_agent(
            agent_id=agent_id,
            name=name,
            parent_id=parent_id,
            task=task,
            skills=list(skills),
            status="running",
        )
        runtime.task = asyncio.create_task(self._run_agent(runtime))
        self.child_tasks.add(runtime.task)
        runtime.task.add_done_callback(self.child_tasks.discard)
        return {"success": True, "agent_id": agent_id, "status": "running"}

    def _agent_context(self, runtime: _AgentRuntime) -> dict[str, Any]:
        from strix.config import load_settings

        assert self.bundle is not None
        return {
            "coordinator": self.coordinator,
            "sandbox_session": self.bundle["session"],
            "caido_client": self.bundle["caido_client"],
            "agent_id": runtime.agent_id,
            "parent_id": runtime.parent_id,
            "task": runtime.task_text,
            "interactive": False,
            "spawn_child_agent": self._spawn_child,
            "max_context_images": load_settings().runtime.max_context_images,
        }

    async def _mailbox_input(self, agent_id: str) -> tuple[str | None, int]:
        async with self.coordinator._lock:
            runtime = self.coordinator.runtimes[agent_id]
            queued = [dict(item) for item in runtime.mailbox]
        if not queued:
            return None, 0
        parts = []
        for message in queued:
            sender = str(message.get("from", "unknown"))
            sender_name = self.coordinator.names.get(sender, sender)
            parts.append(
                f"[Message from {sender_name} ({sender}) | "
                f"type={message.get('type', 'information')} | "
                f"priority={message.get('priority', 'normal')}]\n{message.get('content', '')}"
            )
        return "\n\n".join(parts), len(queued)

    async def _ack_mailbox(self, agent_id: str, count: int) -> None:
        async with self.coordinator._lock:
            runtime = self.coordinator.runtimes[agent_id]
            del runtime.mailbox[:count]
            self.coordinator.pending_counts[agent_id] = len(runtime.mailbox)
        await self.coordinator._maybe_snapshot()

    def _checkpoint(self, runtime: _AgentRuntime, spec: Any, result: Any) -> SessionCheckpoint:
        schemas = {
            str(getattr(tool, "name", "")): dict(getattr(tool, "params_json_schema", {}))
            for tool in runtime._tools  # type: ignore[attr-defined]
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
            journal_clean=bool(self.journal and self.journal.is_clean),
        )

    async def _run_agent(self, runtime: _AgentRuntime, *, resume: bool = False) -> None:
        from strix.core.execution import notify_parent_on_terminal

        assert self.bundle is not None and self.store is not None and self.journal is not None
        status = "failed"
        try:
            prompt, tools = _effective_agent_and_tools(
                sandbox_session=self.bundle["session"],
                scan_config=self.config.scan_config,
                name=runtime.name,
                skills=runtime.skills,
                is_root=runtime.parent_id is None,
            )
            runtime._tools = tools  # type: ignore[attr-defined]
            cwd = self.store.state_dir / f"claude-{runtime.agent_id}"
            cwd.mkdir(parents=True, exist_ok=True)
            checkpoint = self.store.checkpoint_for(runtime.agent_id) if resume else None
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
                    journal=self.journal,
                    max_tool_calls=self.config.max_tool_calls_per_agent,
                    parking_semaphore=self.semaphore,
                )
            )
            session = self.backend.create_session(spec)
            runtime.session = session
            handle = _SessionInterruptHandle(session)
            await self.coordinator.attach_stream(runtime.agent_id, handle)
            await self.coordinator.attach_runtime(
                runtime.agent_id,
                session=_MailboxSessionMirror(),
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
                        await self._emit(event)
                        if event.kind == "rate_limit" and event.payload.get("status") == "rejected":
                            raise SingleAgentScanError(
                                "Claude subscription rate or plan limit reached; retry after "
                                "capacity resets or lower --max-concurrent-agents"
                            )
                result = session.result
                if result is None:
                    raise SingleAgentScanError("Claude agent ended without a terminal result")
                self.store.record_usage(
                    result.usage,
                    model=self.config.model,
                    observed_models=result.models,
                )
                _record_subscription_usage(
                    self.report_state,
                    agent_id=runtime.agent_id,
                    agent_name=runtime.name,
                    result=result,
                    model=self.config.model,
                )
                runtime.turns_used += int(result.turns or 0)
                if result.provider_session_id:
                    checkpoint = self._checkpoint(runtime, spec, result)
                    self.store.record_agent(
                        agent_id=runtime.agent_id,
                        name=runtime.name,
                        parent_id=runtime.parent_id,
                        task=runtime.task_text,
                        skills=runtime.skills,
                        status=str(self.coordinator.statuses.get(runtime.agent_id, "running")),
                        checkpoint=checkpoint,
                    )
                current_status = str(self.coordinator.statuses.get(runtime.agent_id, "running"))
                if runtime.parent_id is None and self.report_state.scan_results:
                    status = "completed"
                    await self.coordinator.set_status(runtime.agent_id, status)
                    return
                if runtime.parent_id is not None and current_status == "completed":
                    status = "completed"
                    return
                if current_status == "stopped":
                    status = "stopped"
                    return
                mailbox_text, count = await self._mailbox_input(runtime.agent_id)
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
                        and self.journal.is_agent_clean(runtime.agent_id)
                    )
                    if not recoverable_wait:
                        raise SingleAgentScanError(
                            f"Claude agent {runtime.agent_id} failed ({result.terminal_reason})"
                        )
                if mailbox_text:
                    current_history.append({"role": "user", "content": mailbox_text})
                    async with self.semaphore:
                        await session.continue_with(mailbox_text)
                    await self._ack_mailbox(runtime.agent_id, count)
                    recovery = 0
                    continue
                if runtime.parent_id is None and any(
                    item.parent_id == runtime.agent_id and item.task and not item.task.done()
                    for item in self.runtimes.values()
                ):
                    await self.coordinator.wait_for_message(runtime.agent_id, timeout=1.0)
                    mailbox_text, count = await self._mailbox_input(runtime.agent_id)
                    if mailbox_text:
                        current_history.append({"role": "user", "content": mailbox_text})
                        async with self.semaphore:
                            await session.continue_with(mailbox_text)
                        await self._ack_mailbox(runtime.agent_id, count)
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
                await self._emit(
                    BackendEvent(
                        "sdk_cleanup_failed",
                        runtime.agent_id,
                        {"error_type": type(close_error).__name__},
                    )
                )
            if runtime.parent_id is not None:
                await notify_parent_on_terminal(self.coordinator, runtime.agent_id, status)
            self.store.record_agent(
                agent_id=runtime.agent_id,
                name=runtime.name,
                parent_id=runtime.parent_id,
                task=runtime.task_text,
                skills=runtime.skills,
                status=status,
                checkpoint=self.store.checkpoint_for(runtime.agent_id),
            )
            if close_error is not None and not active_exception and runtime.parent_id is None:
                raise close_error

    async def run(self) -> SingleAgentScanOutcome:
        from strix.core.agents import AgentCoordinator
        from strix.core.inputs import build_root_task
        from strix.core.paths import run_dir_for, runtime_state_dir
        from strix.report.state import (
            ReportState,
            get_global_report_state,
            set_global_report_state,
        )
        from strix.runtime import session_manager
        from strix.tools.output_store import WORKSPACE_SPILL_DIR, configure_spill_writer

        verify_runtime_compatibility()
        run_dir = run_dir_for(self.config.run_name)
        state_dir = runtime_state_dir(run_dir)
        resume = self.config.resume_token is not None
        if resume:
            if not run_dir.is_dir():
                raise SingleAgentScanError("resume run directory does not exist")
        elif run_dir.exists():
            raise SingleAgentScanError(
                "run directory already exists; provide an explicit resume token"
            )
        self.store = RunStateStore(state_dir / "claude-bridge")
        if resume:
            self.store.open_resume(str(self.config.resume_token))
        else:
            self.store.initialize(
                run_name=self.config.run_name,
                auth_mode=(
                    "simulated_no_auth" if self.simulated_inference else "claude_subscription"
                ),
            )
        secure_run_tree(run_dir)
        self.journal = ToolInvocationJournal(self.store.journal_path, replay_mode=resume)

        self.report_state = ReportState(self.config.run_name)
        if resume:
            self.report_state.hydrate_from_run_dir()
        self.report_state.set_scan_config(dict(self.config.scan_config))
        self.report_state.run_record["auth_mode"] = (
            "simulated_no_auth" if self.simulated_inference else "claude_subscription"
        )
        self.report_state.run_record["execution_backend"] = "claude-agent-sdk"
        self.report_state.run_record["max_concurrent_agents"] = self.config.max_concurrent_agents
        self.report_state.run_record["max_runtime_seconds"] = self.config.max_runtime_s
        self.report_state._llm_usage.zero_cost = True
        self.report_state.save_run_data()
        previous_report = get_global_report_state()
        set_global_report_state(self.report_state)
        tool_state = _isolated_strix_tool_state(state_dir)
        tool_state.__enter__()
        first_error: BaseException | None = None
        completed = False
        terminal_reason = "not_started"
        try:
            Coordinator = type(
                "BridgeAgentCoordinator", (_DurableCoordinatorMixin, AgentCoordinator), {}
            )
            self.coordinator = Coordinator()
            self.coordinator.set_snapshot_path(self.store.state_dir / "agent-graph.json")
            self.bundle = await _create_strix_bundle(
                session_manager,
                self.config.run_name,
                image=self.config.image,
                local_sources=[dict(item) for item in self.config.local_sources],
            )
            await self._emit(
                BackendEvent(
                    "sandbox_ready",
                    self.root_id,
                    {"runtime": "strix", "image": self.config.image},
                )
            )
            sandbox = self.bundle["session"]

            async def spill(output_id: str, text: str) -> str | None:
                path = f"{WORKSPACE_SPILL_DIR}/{output_id}.txt"
                try:
                    await sandbox.write(Path(path), io.BytesIO(text.encode()))
                except Exception:
                    return None
                return path

            configure_spill_writer(spill)
            root_task = build_root_task(dict(self.config.scan_config))
            deadline = asyncio.get_running_loop().time() + self.config.max_runtime_s
            if resume:
                saved = self.store.agents
                for agent_id, value in saved.items():
                    if not isinstance(value, Mapping):
                        continue
                    runtime = _AgentRuntime(
                        str(agent_id),
                        str(value.get("name") or agent_id),
                        value.get("parent_id"),
                        str(value.get("task") or root_task),
                        list(value.get("skills") or []),
                        (),
                    )
                    checkpoint = self.store.checkpoint_for(str(agent_id))
                    if checkpoint is not None:
                        runtime.turns_used = checkpoint.last_settled_turn
                    self.runtimes[str(agent_id)] = runtime
                    await self.coordinator.register(
                        str(agent_id),
                        runtime.name,
                        runtime.parent_id,
                        task=runtime.task_text,
                        skills=runtime.skills,
                    )
                roots = [item for item in self.runtimes.values() if item.parent_id is None]
                if len(roots) != 1:
                    raise SingleAgentScanError("resume state must contain exactly one root agent")
                root = roots[0]
                self.root_id = root.agent_id
                if str(saved[root.agent_id].get("status")) == "completed":
                    raise SingleAgentScanError("completed scans cannot be resumed")
                active = [
                    item
                    for item in self.runtimes.values()
                    if str(saved[item.agent_id].get("status")) != "completed"
                ]
                for item in active:
                    item.task = asyncio.create_task(self._run_agent(item, resume=True))
                    if item.parent_id is not None:
                        self.child_tasks.add(item.task)
                assert root.task is not None
                await asyncio.wait_for(
                    root.task,
                    timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                )
            else:
                root = _AgentRuntime(self.root_id, "Root Agent", None, root_task, [], ())
                self.runtimes[root.agent_id] = root
                await self.coordinator.register(
                    root.agent_id, root.name, None, task=root.task_text, skills=[]
                )
                self.store.record_agent(
                    agent_id=root.agent_id,
                    name=root.name,
                    parent_id=None,
                    task=root.task_text,
                    skills=[],
                    status="running",
                )
                with _deterministic_report_dedupe():
                    root.task = asyncio.create_task(self._run_agent(root))
                    await asyncio.wait_for(
                        root.task,
                        timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                    )
            if self.child_tasks:
                await asyncio.wait_for(
                    asyncio.gather(*list(self.child_tasks), return_exceptions=True),
                    timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                )
            completed = bool(
                self.report_state.scan_results
                and self.report_state.scan_results.get("scan_completed")
            )
            terminal_reason = "completed" if completed else "incomplete"
            if not completed:
                raise SingleAgentScanError("root agent ended without finish_scan")
        except TimeoutError as exc:
            first_error = SingleAgentScanError(
                f"scan exceeded --max-runtime ({self.config.max_runtime_s:g}s)"
            )
            first_error.__cause__ = exc
        except BaseException as exc:
            first_error = exc
        finally:
            self._shutting_down = True
            configure_spill_writer(None)
            tasks = [
                item.task for item in self.runtimes.values() if item.task and not item.task.done()
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if not completed:
                self.report_state.cleanup(
                    status="interrupted"
                    if isinstance(first_error, asyncio.CancelledError)
                    else "failed"
                )
            if self.config.cleanup_on_exit and self.bundle is not None:
                await self._emit(
                    BackendEvent("sandbox_cleanup_attempted", self.root_id, {"runtime": "strix"})
                )
                try:
                    await _cleanup_strix_bundle(session_manager, self.config.run_name, self.bundle)
                except BaseException as exc:
                    await self._emit(
                        BackendEvent(
                            "sandbox_cleanup_failed",
                            self.root_id,
                            {"runtime": "strix", "error_type": type(exc).__name__},
                        )
                    )
                    if first_error is None:
                        first_error = exc
                else:
                    await self._emit(
                        BackendEvent(
                            "sandbox_closed",
                            self.root_id,
                            {"runtime": "strix", "deletion_verified": True},
                        )
                    )
            secure_run_tree(run_dir)
            tool_state.__exit__(None, None, None)
            set_global_report_state(previous_report)
        if first_error is not None:
            raise first_error
        return SingleAgentScanOutcome(
            run_name=self.config.run_name,
            run_dir=run_dir,
            completed=completed,
            vulnerability_count=len(self.report_state.vulnerability_reports),
            terminal_reason=terminal_reason,
            simulated_inference=self.simulated_inference,
        )


async def run_multi_agent_scan(
    config: MultiAgentScanConfig,
    *,
    backend: Any | None = None,
    event_sink: Callable[[BackendEvent], Awaitable[None]] | None = None,
    simulated_inference: bool = False,
) -> SingleAgentScanOutcome:
    """Run one root and independently owned child Claude sessions."""
    if backend is None and not simulated_inference:
        require_subscription_environment()
    return await MultiAgentScanRunner(
        config,
        backend=backend,
        event_sink=event_sink,
        simulated_inference=simulated_inference,
    ).run()
