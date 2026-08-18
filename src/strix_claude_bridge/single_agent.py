"""Experimental single-agent Strix scan composition for Claude Agent SDK."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import io
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strix_claude_bridge.auth import require_subscription_environment
from strix_claude_bridge.backend import BackendEvent, ExecutionBackend
from strix_claude_bridge.claude_backend import ClaudeAgentSDKBackend
from strix_claude_bridge.runtime_state import secure_run_tree
from strix_claude_bridge.strix_integration import (
    StrixAgentBridgeInput,
    build_claude_session_spec,
    verify_runtime_compatibility,
)

BridgeEventSink = Callable[[BackendEvent], Awaitable[None]]


class SingleAgentScanError(RuntimeError):
    """A user-actionable compatibility or scan-lifecycle failure."""


@dataclass(frozen=True)
class SingleAgentScanConfig:
    """Resolved Strix inputs for one experimental Claude-backed root agent."""

    scan_config: Mapping[str, Any]
    run_name: str
    image: str
    local_sources: Sequence[Mapping[str, Any]] = ()
    extra_files: Sequence[Mapping[str, Any]] = ()
    model: str | None = None
    max_turns: int = 100
    turn_timeout_s: float | None = None
    recovery_turns: int = 2
    cleanup_on_exit: bool = True

    def __post_init__(self) -> None:
        if not self.run_name.strip():
            raise ValueError("run_name must not be empty")
        if not self.image.strip():
            raise ValueError("Strix sandbox image must not be empty")
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.recovery_turns < 0:
            raise ValueError("recovery_turns must not be negative")


@dataclass(frozen=True)
class SingleAgentScanOutcome:
    run_name: str
    run_dir: Path
    completed: bool
    vulnerability_count: int
    terminal_reason: str
    simulated_inference: bool = False


def _single_agent_prompt(root_prompt: str) -> str:
    """Remove Strix root-only delegation rules from its rendered methodology prompt."""
    prompt = re.sub(
        r"\s*<root_agent_directive>.*?</root_agent_directive>\s*",
        "\n",
        root_prompt,
        flags=re.DOTALL,
    )
    prompt = re.sub(
        r"\s*<multi_agent_system>.*?</multi_agent_system>\s*",
        "\n",
        prompt,
        flags=re.DOTALL,
    )
    prompt = re.sub(
        r"INTER-AGENT MESSAGES:.*?(?=AUTONOMOUS BEHAVIOR:|INTERACTIVE BEHAVIOR:)",
        "",
        prompt,
        flags=re.DOTALL,
    )
    delegation_terms = re.compile(
        r"\b(?:sub-?agents?|create_agent|agent_finish|wait_for_agents|"
        r"send_message_to_agent|view_agent_graph|delegat\w*)\b",
        flags=re.IGNORECASE,
    )
    prompt = "\n".join(
        line for line in prompt.splitlines() if not delegation_terms.search(line)
    ).strip()
    if delegation_terms.search(prompt):
        raise SingleAgentScanError("Strix single-agent prompt still contains delegation directives")
    return (
        prompt + "\n\n<single_agent_directive>\n"
        "You are the only agent in this experimental backend. Perform all authorized "
        "assessment work directly with the available tools. Do not attempt multi-agent "
        "coordination. End the assessment by calling finish_scan exactly once.\n"
        "</single_agent_directive>"
    )


def _effective_root_agent_and_tools(
    *,
    sandbox_session: Any,
    scan_config: Mapping[str, Any],
) -> tuple[str, list[Any]]:
    """Render Strix's root prompt and export its real effective root tools."""
    from strix.agents.factory import build_strix_agent
    from strix.core.inputs import build_scope_context

    targets = list(scan_config.get("targets") or [])
    scan_mode = str(scan_config.get("scan_mode") or "deep")
    is_whitebox = any(item.get("type") == "local_code" for item in targets)
    skills = list(scan_config.get("skills") or [])
    agent = build_strix_agent(
        name="Root Agent",
        skills=skills,
        is_root=True,
        scan_mode=scan_mode,
        is_whitebox=is_whitebox,
        interactive=False,
        # This asks Strix to expose native custom tools as FunctionTools. The
        # bridge still invokes the resulting original implementations directly.
        chat_completions_tools=True,
        system_prompt_context=build_scope_context(dict(scan_config)),
    )
    root_prompt = str(agent.instructions or "")
    if not root_prompt.strip():
        raise SingleAgentScanError("Strix rendered an empty root prompt")
    prompt = _single_agent_prompt(root_prompt)

    tools = list(agent.tools)
    for capability in agent.capabilities:
        bound = capability.clone()
        bound.bind(sandbox_session)
        for tool in bound.tools():
            # Strix converts apply_patch from a CustomTool whose construction has
            # static no-approval policy. Its compatibility wrapper represents that
            # as a callback; restore the original static policy for this MCP seam.
            if getattr(tool, "name", None) == "apply_patch":
                tool.needs_approval = False
            tools.append(tool)
    names = [getattr(tool, "name", "") for tool in tools]
    if len(names) != len(set(names)) or any(not name for name in names):
        raise SingleAgentScanError("Strix produced invalid or duplicate effective root tools")
    required = {"exec_command", "apply_patch", "finish_scan", "create_vulnerability_report"}
    missing = sorted(required - set(names))
    if missing:
        raise SingleAgentScanError(
            "Strix effective root tool set is missing: " + ", ".join(missing)
        )
    return prompt, tools


@contextlib.contextmanager
def _deterministic_report_dedupe() -> Any:
    """Prevent Claude scans from silently re-entering Strix's LiteLLM dedupe path."""
    from strix.report import dedupe as dedupe_module

    original = dedupe_module.check_duplicate

    async def check_duplicate(
        candidate: dict[str, Any], existing: list[dict[str, Any]], **_kwargs: Any
    ) -> dict[str, Any]:
        def identity(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
            return tuple(
                str(item.get(key) or "").strip().casefold()
                for key in ("title", "target", "endpoint", "method")
            )

        candidate_identity = identity(candidate)
        match = next((item for item in existing if identity(item) == candidate_identity), None)
        return {
            "is_duplicate": match is not None,
            "duplicate_id": str(match.get("id") or "") if match else "",
            "confidence": 1.0 if match else 0.0,
            "reason": "deterministic exact finding identity" if match else "",
        }

    dedupe_module.check_duplicate = check_duplicate
    try:
        yield
    finally:
        dedupe_module.check_duplicate = original


@contextlib.contextmanager
def _isolated_strix_tool_state(state_dir: Path) -> Any:
    """Bind note/todo module state to this run and restore prior library state."""
    from strix.tools.notes import tools as notes
    from strix.tools.todo import tools as todos

    notes_storage = copy.deepcopy(notes._notes_storage)
    notes_path = notes._notes_path
    todos_storage = copy.deepcopy(todos._todos_storage)
    todos_path = todos._todos_path
    notes.hydrate_notes_from_disk(state_dir)
    todos.hydrate_todos_from_disk(state_dir)
    try:
        yield
    finally:
        with notes._notes_lock:
            notes._notes_storage.clear()
            notes._notes_storage.update(notes_storage)
            notes._notes_path = notes_path
        with todos._todos_io_lock:
            todos._todos_storage.clear()
            todos._todos_storage.update(todos_storage)
            todos._todos_path = todos_path


async def _delete_strix_session(
    client: Any, session: Any, *, caido_client: Any | None = None
) -> None:
    """Delete a Strix session without swallowing the authoritative delete result."""
    if caido_client is not None:
        with contextlib.suppress(Exception):
            await caido_client.aclose()
    try:
        await client.delete(session)
    finally:
        docker_client = getattr(client, "docker_client", None)
        if docker_client is not None:
            with contextlib.suppress(Exception):
                docker_client.close()


async def _create_strix_bundle(session_manager: Any, scan_id: str, **kwargs: Any) -> dict[str, Any]:
    """Own and delete a session if Strix fails between creation and cache publication."""
    original_get_backend = session_manager.get_backend
    created: dict[str, Any] = {}

    def tracking_get_backend(backend_name: str) -> Any:
        backend = original_get_backend(backend_name)

        async def tracking_backend(**backend_kwargs: Any) -> tuple[Any, Any]:
            client, session = await backend(**backend_kwargs)
            created.update(client=client, session=session)
            return client, session

        return tracking_backend

    session_manager.get_backend = tracking_get_backend
    try:
        return await session_manager.create_or_reuse(scan_id, **kwargs)
    except BaseException as creation_error:
        if created:
            try:
                await _delete_strix_session(created["client"], created["session"])
            except BaseException as cleanup_error:
                raise SingleAgentScanError(
                    "Strix sandbox bootstrap failed and the partial sandbox could not be deleted"
                ) from cleanup_error
        raise creation_error
    finally:
        session_manager.get_backend = original_get_backend


async def _cleanup_strix_bundle(
    session_manager: Any, scan_id: str, bundle: Mapping[str, Any]
) -> None:
    """Remove the cache entry and verify that the owned session deletion succeeds."""
    cache = getattr(session_manager, "_SESSION_CACHE", None)
    if isinstance(cache, dict):
        cache.pop(scan_id, None)
    await _delete_strix_session(
        bundle["client"], bundle["session"], caido_client=bundle.get("caido_client")
    )


def _record_subscription_usage(
    report_state: Any,
    *,
    agent_id: str,
    agent_name: str,
    result: Any,
    model: str | None,
) -> None:
    if result is None:
        return
    from agents.usage import Usage

    usage = dict(result.usage)
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    observed_models = tuple(getattr(result, "models", ()) or ())
    effective_model = (
        observed_models[0] if len(observed_models) == 1 else (None if observed_models else model)
    )
    report_state.record_sdk_usage(
        agent_id=agent_id,
        agent_name=agent_name,
        model=effective_model,
        usage=Usage(
            requests=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


async def run_single_agent_scan(
    config: SingleAgentScanConfig,
    *,
    backend: ExecutionBackend | None = None,
    event_sink: BridgeEventSink | None = None,
    simulated_inference: bool = False,
) -> SingleAgentScanOutcome:
    """Run a complete root-only Strix scan without ``Runner.run_streamed``."""
    verify_runtime_compatibility()
    if backend is None and not simulated_inference:
        require_subscription_environment()

    from strix.config import load_settings
    from strix.core.agents import AgentCoordinator
    from strix.core.inputs import build_root_task
    from strix.core.paths import run_dir_for, runtime_state_dir
    from strix.report.state import ReportState, get_global_report_state, set_global_report_state
    from strix.runtime import session_manager
    from strix.tools.output_store import WORKSPACE_SPILL_DIR, configure_spill_writer

    scan_config = dict(config.scan_config)
    root_task = build_root_task(scan_config)
    if not root_task.strip():
        raise SingleAgentScanError("Strix scan inputs produced an empty root task")

    run_dir = run_dir_for(config.run_name)
    if run_dir.exists():
        raise SingleAgentScanError(f"run directory already exists; resume is disabled: {run_dir}")
    state_dir = runtime_state_dir(run_dir)
    sdk_cwd = state_dir / "claude-root"
    sdk_cwd.mkdir(parents=True, exist_ok=False)
    secure_run_tree(run_dir)

    report_state = ReportState(config.run_name)
    report_state.set_scan_config(scan_config)
    report_state.run_record["auth_mode"] = (
        "simulated_no_auth" if simulated_inference else "claude_subscription"
    )
    report_state._llm_usage.zero_cost = True
    report_state.save_run_data()
    previous_report_state = get_global_report_state()
    set_global_report_state(report_state)
    tool_state = _isolated_strix_tool_state(state_dir)
    tool_state.__enter__()

    coordinator = AgentCoordinator()
    root_id = uuid.uuid4().hex[:8]

    bundle: dict[str, Any] | None = None
    execution_session = None
    terminal_reason = "not_started"
    completed = False
    first_error: BaseException | None = None
    interrupted = False
    try:
        await coordinator.register(
            root_id,
            "Root Agent",
            parent_id=None,
            task=root_task,
            skills=list(scan_config.get("skills") or []),
        )
        create_parameters = inspect.signature(session_manager.create_or_reuse).parameters
        if config.extra_files and "extra_files" not in create_parameters:
            raise SingleAgentScanError(
                "installed Strix cannot place workspace files in its sandbox; install the "
                "compatibility-tested Strix source revision"
            )
        create_kwargs: dict[str, Any] = {
            "image": config.image,
            "local_sources": [dict(item) for item in config.local_sources],
        }
        if "extra_files" in create_parameters:
            create_kwargs["extra_files"] = [dict(item) for item in config.extra_files]
        bundle = await _create_strix_bundle(session_manager, config.run_name, **create_kwargs)
        if event_sink is not None:
            await event_sink(
                BackendEvent(
                    "sandbox_ready",
                    root_id,
                    {"runtime": "strix", "image": config.image},
                )
            )
        sandbox_session = bundle["session"]

        async def spill_to_workspace(output_id: str, text: str) -> str | None:
            path = f"{WORKSPACE_SPILL_DIR}/{output_id}.txt"
            try:
                await sandbox_session.write(Path(path), io.BytesIO(text.encode()))
            except Exception:
                return None
            return path

        configure_spill_writer(spill_to_workspace)

        async def unsupported_child_spawner(**_kwargs: Any) -> dict[str, Any]:
            raise SingleAgentScanError(
                "create_agent is unavailable in the experimental single-agent Claude backend"
            )

        context: dict[str, Any] = {
            "coordinator": coordinator,
            "sandbox_session": sandbox_session,
            "caido_client": bundle["caido_client"],
            "agent_id": root_id,
            "parent_id": None,
            "interactive": False,
            "spawn_child_agent": unsupported_child_spawner,
            "max_context_images": load_settings().runtime.max_context_images,
        }
        prompt, tools = _effective_root_agent_and_tools(
            sandbox_session=sandbox_session,
            scan_config=scan_config,
        )
        spec = build_claude_session_spec(
            StrixAgentBridgeInput(
                agent_id=root_id,
                system_prompt=prompt,
                cwd=sdk_cwd,
                context=context,
                function_tools=tools,
                turn_input=(root_task,),
                model=config.model,
                max_turns=config.max_turns,
                turn_timeout_s=config.turn_timeout_s,
            )
        )
        execution_session = (backend or ClaudeAgentSDKBackend()).create_session(spec)

        with _deterministic_report_dedupe():
            await execution_session.start(root_task)
            for recovery_index in range(config.recovery_turns + 1):
                async for event in execution_session.events():
                    if event_sink is not None:
                        await event_sink(event)
                _record_subscription_usage(
                    report_state,
                    agent_id=root_id,
                    agent_name="Root Agent",
                    result=execution_session.result,
                    model=config.model,
                )
                result = execution_session.result
                terminal_reason = result.terminal_reason if result else "missing_terminal_result"
                if report_state.scan_results and report_state.scan_results.get("scan_completed"):
                    completed = True
                    await coordinator.set_status(root_id, "completed")
                    break
                if result is None or result.is_error:
                    raise SingleAgentScanError(
                        f"Claude scan turn failed ({terminal_reason}); inspect bridge events and "
                        "verify local SDK authentication/capacity"
                    )
                if recovery_index >= config.recovery_turns:
                    raise SingleAgentScanError(
                        "Claude ended without calling finish_scan; recovery turn limit exhausted"
                    )
                await execution_session.continue_with(
                    "The scan is not complete because finish_scan has not succeeded. Continue the "
                    "authorized assessment, create reports for verified findings, then call "
                    "finish_scan exactly once with final prose."
                )
    except asyncio.CancelledError as exc:
        first_error = exc
        interrupted = True
        if execution_session is not None:
            with contextlib.suppress(BaseException):
                await execution_session.cancel()
        with contextlib.suppress(Exception):
            await coordinator.set_status(root_id, "interrupted")
    except BaseException as exc:
        first_error = exc
        with contextlib.suppress(Exception):
            await coordinator.set_status(root_id, "failed")
    finally:
        configure_spill_writer(None)
        if execution_session is not None:
            try:
                await execution_session.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if not completed:
            with contextlib.suppress(Exception):
                status = "interrupted" if interrupted else ("failed" if first_error else "stopped")
                report_state.cleanup(status=status)
        if config.cleanup_on_exit and bundle is not None:
            if event_sink is not None:
                with contextlib.suppress(BaseException):
                    await event_sink(
                        BackendEvent(
                            "sandbox_cleanup_attempted",
                            root_id,
                            {"runtime": "strix"},
                        )
                    )
            try:
                await _cleanup_strix_bundle(session_manager, config.run_name, bundle)
            except BaseException as exc:
                if event_sink is not None:
                    with contextlib.suppress(BaseException):
                        await event_sink(
                            BackendEvent(
                                "sandbox_cleanup_failed",
                                root_id,
                                {"runtime": "strix", "error_type": type(exc).__name__},
                            )
                        )
                if first_error is None:
                    first_error = exc
            else:
                if event_sink is not None:
                    with contextlib.suppress(BaseException):
                        await event_sink(
                            BackendEvent(
                                "sandbox_closed",
                                root_id,
                                {"runtime": "strix", "deletion_verified": True},
                            )
                        )
        secure_run_tree(run_dir)
        tool_state.__exit__(None, None, None)
        set_global_report_state(previous_report_state)

    if first_error is not None:
        raise first_error
    return SingleAgentScanOutcome(
        run_name=config.run_name,
        run_dir=run_dir,
        completed=completed,
        vulnerability_count=len(report_state.vulnerability_reports),
        terminal_reason=terminal_reason,
        simulated_inference=simulated_inference,
    )
