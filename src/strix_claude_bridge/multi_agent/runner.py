"""Experimental multi-agent Strix coordinator backed by one Claude session per agent."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strix_claude_bridge.auth import require_subscription_environment
from strix_claude_bridge.backend import BackendEvent
from strix_claude_bridge.claude_backend import ClaudeAgentSDKBackend
from strix_claude_bridge.runtime_state import secure_run_tree
from strix_claude_bridge.single_agent import (
    BridgeEventSink,
    SingleAgentScanError,
    SingleAgentScanOutcome,
    _cleanup_strix_bundle,
    _create_strix_bundle,
    _deterministic_report_dedupe,
    _isolated_strix_tool_state,
)

from .execution import (  # noqa: F401
    AgentRuntime,
    MultiAgentAgentExecutor,
    _effective_agent_and_tools,
)
from .state import MultiAgentRunArtifacts, build_multi_agent_run_artifacts


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

    def set_snapshot_path(self, path: Path) -> None:  # type: ignore[override]
        super().set_snapshot_path(path)  # type: ignore[misc]
        self._snapshot_paths = [path]

    def set_secondary_snapshot_path(self, path: Path) -> None:
        paths = list(getattr(self, "_snapshot_paths", []))
        if path not in paths:
            paths.append(path)
        self._snapshot_paths = paths

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

    async def _maybe_snapshot(self) -> None:  # type: ignore[override]
        paths = [path for path in getattr(self, "_snapshot_paths", []) if path is not None]
        if not paths:
            return
        try:
            data = await self.snapshot()
            payload = json.dumps(data, ensure_ascii=False, default=str)
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=str(path.parent),
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as tmp:
                    tmp.write(payload)
                    tmp_path = Path(tmp.name)
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, path)
                os.chmod(path, 0o600)
        except Exception:
            return

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
        self.runtimes: dict[str, AgentRuntime] = {}
        self.child_tasks: set[asyncio.Task[None]] = set()
        self.bundle: Mapping[str, Any] | None = None
        self.coordinator: Any = None
        self.artifacts: MultiAgentRunArtifacts | None = None
        self.agent_executor: MultiAgentAgentExecutor | None = None
        self.root_id = "root0001"
        self._agent_counter = 1
        self._shutting_down = False

    def _require_artifacts(self) -> MultiAgentRunArtifacts:
        assert self.artifacts is not None
        return self.artifacts

    def _require_executor(self) -> MultiAgentAgentExecutor:
        assert self.agent_executor is not None
        return self.agent_executor

    async def _emit(self, event: BackendEvent) -> None:
        await self._require_artifacts().emit(event)

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
        runtime = AgentRuntime(
            agent_id,
            name,
            parent_id,
            task,
            list(skills),
            tuple(parent_history[-20:]),
        )
        self.runtimes[agent_id] = runtime
        await self.coordinator.register(agent_id, name, parent_id, task=task, skills=list(skills))
        self._require_artifacts().record_agent(
            agent_id=agent_id,
            name=name,
            parent_id=parent_id,
            task=task,
            skills=list(skills),
            status="running",
        )
        runtime.task = asyncio.create_task(self._require_executor().run(runtime))
        self.child_tasks.add(runtime.task)
        runtime.task.add_done_callback(self.child_tasks.discard)
        return {"success": True, "agent_id": agent_id, "status": "running"}

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        return max(0.001, deadline - asyncio.get_running_loop().time())

    async def _restore_resumed_runtimes(self, root_task: str) -> AgentRuntime:
        artifacts = self._require_artifacts()
        saved = artifacts.store.agents
        for agent_id, value in saved.items():
            if not isinstance(value, Mapping):
                continue
            runtime = AgentRuntime(
                str(agent_id),
                str(value.get("name") or agent_id),
                value.get("parent_id"),
                str(value.get("task") or root_task),
                list(value.get("skills") or []),
                (),
            )
            checkpoint = artifacts.checkpoint_for(str(agent_id))
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
            item.task = asyncio.create_task(self._require_executor().run(item, resume=True))
            if item.parent_id is not None:
                self.child_tasks.add(item.task)
        assert root.task is not None
        return root

    async def _start_fresh_root(self, root_task: str) -> AgentRuntime:
        root = AgentRuntime(self.root_id, "Root Agent", None, root_task, [], ())
        self.runtimes[root.agent_id] = root
        await self.coordinator.register(
            root.agent_id, root.name, None, task=root.task_text, skills=[]
        )
        self._require_artifacts().record_agent(
            agent_id=root.agent_id,
            name=root.name,
            parent_id=None,
            task=root.task_text,
            skills=[],
            status="running",
        )
        root.task = asyncio.create_task(self._require_executor().run(root))
        return root

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

        from strix_claude_bridge import multi_agent as multi_agent_package

        multi_agent_package.verify_runtime_compatibility()
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

        report_state = ReportState(self.config.run_name)
        if resume:
            report_state.hydrate_from_run_dir()
        report_state.set_scan_config(dict(self.config.scan_config))
        report_state.run_record["auth_mode"] = (
            "simulated_no_auth" if self.simulated_inference else "claude_subscription"
        )
        report_state.run_record["execution_backend"] = "claude-agent-sdk"
        report_state.run_record["max_concurrent_agents"] = self.config.max_concurrent_agents
        report_state.run_record["max_runtime_seconds"] = self.config.max_runtime_s
        report_state._llm_usage.zero_cost = True
        report_state.save_run_data()
        previous_report = get_global_report_state()
        set_global_report_state(report_state)
        tool_state = _isolated_strix_tool_state(state_dir)
        tool_state.__enter__()

        first_error: BaseException | None = None
        completed = False
        terminal_reason = "not_started"
        try:
            self.artifacts = build_multi_agent_run_artifacts(
                state_dir,
                run_name=self.config.run_name,
                auth_mode=(
                    "simulated_no_auth" if self.simulated_inference else "claude_subscription"
                ),
                resume=resume,
                resume_token=self.config.resume_token,
                report_state=report_state,
                event_sink=self.event_sink,
            )
            secure_run_tree(run_dir)

            Coordinator = type(
                "BridgeAgentCoordinator", (_DurableCoordinatorMixin, AgentCoordinator), {}
            )
            self.coordinator = Coordinator()
            self.coordinator.set_snapshot_path(state_dir / "agents.json")
            self.coordinator.set_secondary_snapshot_path(
                self._require_artifacts().bridge_state_dir / "agent-graph.json"
            )
            self.bundle = await _create_strix_bundle(
                session_manager,
                self.config.run_name,
                image=self.config.image,
                local_sources=[dict(item) for item in self.config.local_sources],
            )
            self.agent_executor = MultiAgentAgentExecutor(
                config=self.config,
                backend=self.backend,
                semaphore=self.semaphore,
                coordinator=self.coordinator,
                bundle=self.bundle,
                artifacts=self._require_artifacts(),
                runtimes=self.runtimes,
                spawn_child=self._spawn_child,
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
                root = await self._restore_resumed_runtimes(root_task)
                await asyncio.wait_for(root.task, timeout=self._remaining_timeout(deadline))
            else:
                with _deterministic_report_dedupe():
                    root = await self._start_fresh_root(root_task)
                    await asyncio.wait_for(root.task, timeout=self._remaining_timeout(deadline))
            if self.child_tasks:
                await asyncio.wait_for(
                    asyncio.gather(*list(self.child_tasks), return_exceptions=True),
                    timeout=self._remaining_timeout(deadline),
                )
            completed = bool(
                report_state.scan_results and report_state.scan_results.get("scan_completed")
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
                report_state.cleanup(
                    status="interrupted"
                    if isinstance(first_error, asyncio.CancelledError)
                    else "failed"
                )
            if (
                self.config.cleanup_on_exit
                and self.bundle is not None
                and self.artifacts is not None
            ):
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
            if self.artifacts is not None:
                self.artifacts.close()
            set_global_report_state(previous_report)
        if first_error is not None:
            raise first_error
        return SingleAgentScanOutcome(
            run_name=self.config.run_name,
            run_dir=run_dir,
            completed=completed,
            vulnerability_count=len(report_state.vulnerability_reports),
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
    from strix_claude_bridge import multi_agent as multi_agent_package

    return await multi_agent_package.MultiAgentScanRunner(
        config,
        backend=backend,
        event_sink=event_sink,
        simulated_inference=simulated_inference,
    ).run()
