from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from strix_claude_bridge.backend import BackendResult, ExecutionBackend, ExecutionSession
from strix_claude_bridge.claude_backend import ClaudeAgentSDKBackend
from strix_claude_bridge.dry_run import build_dry_run_client_factory
from strix_claude_bridge.single_agent import (
    SingleAgentScanConfig,
    SingleAgentScanError,
    _create_strix_bundle,
    _effective_root_agent_and_tools,
    _record_subscription_usage,
    run_single_agent_scan,
)


class FakeSandboxSession:
    def supports_pty(self) -> bool:
        return False

    async def exec(self, *_command: object, **_kwargs: object) -> Any:
        from agents.sandbox.types import ExecResult

        return ExecResult(
            stdout=b"9: # STRIX_DRY_RUN_PATH_TRAVERSAL: fixture evidence\n",
            stderr=b"",
            exit_code=0,
        )

    async def write(self, _path: Path, _data: object) -> None:
        return


class FakeCaido:
    async def aclose(self) -> None:
        return


class FakeSandboxClient:
    def __init__(self, *, delete_error: BaseException | None = None) -> None:
        self.delete_error = delete_error
        self.deleted: list[object] = []

    async def delete(self, session: object) -> None:
        self.deleted.append(session)
        if self.delete_error is not None:
            raise self.delete_error


def scan_config(fixture: Path) -> dict[str, Any]:
    return {
        "targets": [
            {
                "type": "local_code",
                "details": {
                    "target_path": str(fixture),
                    "workspace_subdir": "vulnerable_app",
                },
                "original": str(fixture),
            }
        ],
        "user_instructions": "Inspect only the authorized fixture.",
        "scan_mode": "quick",
        "skills": [],
    }


def test_usage_uses_real_agent_name_and_observed_model_with_safe_fallback() -> None:
    class Report:
        def __init__(self) -> None:
            self.records: list[dict[str, Any]] = []

        def record_sdk_usage(self, **kwargs: Any) -> None:
            self.records.append(kwargs)

    report = Report()
    _record_subscription_usage(
        report,
        agent_id="child-2",
        agent_name="Specialist",
        result=BackendResult("session", "completed", False, {}, 1, ("provider-model",)),
        model="configured-selector",
    )
    _record_subscription_usage(
        report,
        agent_id="root",
        agent_name="Root Agent",
        result=BackendResult("session", "completed", False, {}, 1),
        model=None,
    )
    _record_subscription_usage(
        report,
        agent_id="root-configured",
        agent_name="Root Agent",
        result=BackendResult("session", "completed", False, {}, 1),
        model="configured-selector",
    )
    _record_subscription_usage(
        report,
        agent_id="multi-model",
        agent_name="Specialist",
        result=BackendResult("session", "completed", False, {}, 1, ("provider-a", "provider-b")),
        model="configured-selector",
    )

    assert report.records[0]["agent_id"] == "child-2"
    assert report.records[0]["agent_name"] == "Specialist"
    assert report.records[0]["model"] == "provider-model"
    assert report.records[1]["model"] is None
    assert report.records[2]["model"] == "configured-selector"
    assert report.records[3]["model"] is None


def test_exports_real_strix_root_and_sandbox_capability_tools() -> None:
    prompt, tools = _effective_root_agent_and_tools(
        sandbox_session=FakeSandboxSession(),
        scan_config=scan_config(Path("/fixture")),
    )
    names = {tool.name for tool in tools}

    assert "Authorization source: strix_platform_verified_targets" in prompt
    assert "<single_agent_directive>" in prompt
    for forbidden in ("create_agent", "subagent", "delegat", "agent_finish", "wait_for_agents"):
        assert forbidden not in prompt.casefold()
    assert {
        "exec_command",
        "apply_patch",
        "view_image",
        "finish_scan",
        "create_vulnerability_report",
        "list_requests",
    } <= names
    assert len(names) == len(tools)


@pytest.mark.asyncio
async def test_fake_agent_sdk_runs_real_strix_report_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    monkeypatch.chdir(tmp_path)

    from strix.runtime import session_manager

    from strix_claude_bridge import single_agent, strix_integration

    client = FakeSandboxClient()

    async def create_or_reuse(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "session": FakeSandboxSession(),
            "client": client,
            "caido_client": FakeCaido(),
        }

    monkeypatch.setattr(session_manager, "create_or_reuse", create_or_reuse)
    monkeypatch.setattr(single_agent, "verify_runtime_compatibility", lambda: None)
    monkeypatch.setattr(strix_integration, "verify_runtime_compatibility", lambda: None)

    outcome = await run_single_agent_scan(
        SingleAgentScanConfig(
            scan_config=scan_config(fixture),
            run_name="fake-sdk-e2e",
            image="unused-in-test",
            local_sources=[
                {
                    "source_path": str(fixture),
                    "workspace_subdir": "vulnerable_app",
                    "protect_metadata": True,
                }
            ],
            max_turns=4,
        ),
        backend=ClaudeAgentSDKBackend(
            client_factory=build_dry_run_client_factory(workspace_subdir="vulnerable_app"),
            enforce_subscription_environment=False,
        ),
        simulated_inference=True,
    )

    assert outcome.completed is True
    assert outcome.vulnerability_count == 1
    assert len(client.deleted) == 1
    run_dir = tmp_path / "strix_runs" / "fake-sdk-e2e"
    vulnerabilities = json.loads((run_dir / "vulnerabilities.json").read_text())
    run_record = json.loads((run_dir / "run.json").read_text())
    assert vulnerabilities[0]["title"] == "Path traversal from unvalidated user path"
    assert run_record["auth_mode"] == "simulated_no_auth"
    assert run_record["llm_usage"]["cost"] == 0
    assert run_record["scan_results"]["scan_completed"] is True
    assert run_dir.stat().st_mode & 0o777 == 0o700
    assert (run_dir / "run.json").stat().st_mode & 0o777 == 0o600
    assert (run_dir / "vulnerabilities.json").stat().st_mode & 0o777 == 0o600


class ScriptedSession(ExecutionSession):
    def __init__(
        self,
        *,
        result: BackendResult | None = None,
        block: bool = False,
        close_error: BaseException | None = None,
    ) -> None:
        self._result = result
        self.block = block
        self.close_error = close_error
        self.cancelled = False
        self.closed = False
        self.continues = 0

    @property
    def result(self) -> BackendResult | None:
        return self._result

    async def start(self, _initial_input: str) -> None:
        return

    async def _events(self):
        if self.block:
            await asyncio.Future()
        if False:
            yield

    def events(self):
        return self._events()

    async def continue_with(self, _input_text: str) -> None:
        self.continues += 1

    async def inject(self, input_text: str) -> None:
        await self.continue_with(input_text)

    async def interrupt(self) -> None:
        return

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class ScriptedBackend(ExecutionBackend):
    def __init__(self, session: ExecutionSession) -> None:
        self.session = session

    def create_session(self, _spec: object) -> ExecutionSession:
        return self.session


def _patch_fake_sandbox(monkeypatch: pytest.MonkeyPatch, client: FakeSandboxClient) -> None:
    from strix.runtime import session_manager

    from strix_claude_bridge import single_agent, strix_integration

    async def create_or_reuse(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "session": FakeSandboxSession(),
            "client": client,
            "caido_client": FakeCaido(),
        }

    monkeypatch.setattr(session_manager, "create_or_reuse", create_or_reuse)
    monkeypatch.setattr(single_agent, "verify_runtime_compatibility", lambda: None)
    monkeypatch.setattr(strix_integration, "verify_runtime_compatibility", lambda: None)


@pytest.mark.asyncio
async def test_existing_run_directory_is_rejected_without_hydrating_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "strix_runs" / "collision"
    run_dir.mkdir(parents=True)
    (run_dir / "vulnerabilities.json").write_text('[{"id":"old"}]')

    from strix_claude_bridge import single_agent

    monkeypatch.setattr(single_agent, "verify_runtime_compatibility", lambda: None)
    with pytest.raises(SingleAgentScanError, match="resume is disabled"):
        await run_single_agent_scan(
            SingleAgentScanConfig(
                scan_config=scan_config(Path("/fixture")),
                run_name="collision",
                image="unused",
            ),
            backend=ScriptedBackend(ScriptedSession()),
        )
    assert json.loads((run_dir / "vulnerabilities.json").read_text()) == [{"id": "old"}]


@pytest.mark.asyncio
async def test_partial_sandbox_bootstrap_failure_deletes_created_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from strix.runtime import session_manager

    client = FakeSandboxClient()
    session = FakeSandboxSession()

    async def backend(**_kwargs: object) -> tuple[object, object]:
        return client, session

    monkeypatch.setattr(session_manager, "get_backend", lambda _name: backend)

    async def create_or_reuse(*_args: object, **_kwargs: object) -> dict[str, object]:
        selected = session_manager.get_backend("fake")
        await selected()
        raise RuntimeError("caido bootstrap failed")

    monkeypatch.setattr(session_manager, "create_or_reuse", create_or_reuse)
    with pytest.raises(RuntimeError, match="caido bootstrap failed"):
        await _create_strix_bundle(session_manager, "partial", image="unused", local_sources=[])
    assert client.deleted == [session]


@pytest.mark.asyncio
async def test_provider_failure_persists_failed_and_cleans_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = FakeSandboxClient()
    _patch_fake_sandbox(monkeypatch, client)
    session = ScriptedSession(result=BackendResult(None, "provider_error", True))

    with pytest.raises(SingleAgentScanError, match="scan turn failed"):
        await run_single_agent_scan(
            SingleAgentScanConfig(
                scan_config=scan_config(Path("/fixture")),
                run_name="provider-failure",
                image="unused",
            ),
            backend=ScriptedBackend(session),
        )
    record = json.loads((tmp_path / "strix_runs" / "provider-failure" / "run.json").read_text())
    assert record["status"] == "failed"
    assert session.closed is True
    assert len(client.deleted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_method", ["add_vulnerability_report", "update_scan_final_fields"])
async def test_report_or_finish_tool_failure_is_nonzero_and_cleans_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_method: str
) -> None:
    monkeypatch.chdir(tmp_path)
    client = FakeSandboxClient()
    _patch_fake_sandbox(monkeypatch, client)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"

    from strix.report.state import ReportState

    def fail_tool(*_args: object, **_kwargs: object) -> None:
        raise AttributeError("forced report lifecycle failure")

    monkeypatch.setattr(ReportState, failing_method, fail_tool)
    with pytest.raises(SingleAgentScanError, match="scan turn failed"):
        await run_single_agent_scan(
            SingleAgentScanConfig(
                scan_config=scan_config(fixture),
                run_name=f"tool-failure-{failing_method}",
                image="unused",
            ),
            backend=ClaudeAgentSDKBackend(
                client_factory=build_dry_run_client_factory(workspace_subdir="vulnerable_app"),
                enforce_subscription_environment=False,
            ),
            simulated_inference=True,
        )
    assert len(client.deleted) == 1


@pytest.mark.asyncio
async def test_completion_without_finish_is_bounded_and_cleans_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = FakeSandboxClient()
    _patch_fake_sandbox(monkeypatch, client)
    session = ScriptedSession(result=BackendResult("session", "completed", False))

    with pytest.raises(SingleAgentScanError, match="recovery turn limit exhausted"):
        await run_single_agent_scan(
            SingleAgentScanConfig(
                scan_config=scan_config(Path("/fixture")),
                run_name="recovery-exhausted",
                image="unused",
                recovery_turns=1,
            ),
            backend=ScriptedBackend(session),
        )
    assert session.continues == 1
    assert len(client.deleted) == 1


@pytest.mark.asyncio
async def test_close_failure_does_not_prevent_sandbox_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = FakeSandboxClient()
    _patch_fake_sandbox(monkeypatch, client)
    session = ScriptedSession(
        result=BackendResult(None, "provider_error", True), close_error=RuntimeError("close")
    )

    with pytest.raises(SingleAgentScanError):
        await run_single_agent_scan(
            SingleAgentScanConfig(
                scan_config=scan_config(Path("/fixture")),
                run_name="close-failure",
                image="unused",
            ),
            backend=ScriptedBackend(session),
        )
    assert len(client.deleted) == 1


@pytest.mark.asyncio
async def test_outer_cancellation_is_interrupted_and_restores_global_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = FakeSandboxClient()
    _patch_fake_sandbox(monkeypatch, client)
    session = ScriptedSession(block=True)

    from strix.report.state import ReportState, get_global_report_state, set_global_report_state
    from strix.tools import output_store
    from strix.tools.notes import tools as notes
    from strix.tools.todo import tools as todos

    previous = ReportState("previous")
    set_global_report_state(previous)
    notes._notes_storage["prior"] = {"content": "keep"}
    todos._todos_storage["prior"] = {"todo": {"title": "keep"}}

    task = asyncio.create_task(
        run_single_agent_scan(
            SingleAgentScanConfig(
                scan_config=scan_config(Path("/fixture")),
                run_name="cancelled",
                image="unused",
            ),
            backend=ScriptedBackend(session),
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = json.loads((tmp_path / "strix_runs" / "cancelled" / "run.json").read_text())
    assert record["status"] == "interrupted"
    assert session.cancelled is True
    assert session.closed is True
    assert len(client.deleted) == 1
    assert get_global_report_state() is previous
    assert notes._notes_storage == {"prior": {"content": "keep"}}
    assert todos._todos_storage == {"prior": {"todo": {"title": "keep"}}}
    assert "writer" not in output_store._spill


@pytest.mark.asyncio
async def test_delete_failure_emits_failed_not_closed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = FakeSandboxClient(delete_error=RuntimeError("delete failed"))
    _patch_fake_sandbox(monkeypatch, client)
    events: list[str] = []

    async def sink(event: Any) -> None:
        events.append(str(event.kind))

    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    with pytest.raises(RuntimeError, match="delete failed"):
        await run_single_agent_scan(
            SingleAgentScanConfig(
                scan_config=scan_config(fixture),
                run_name="delete-failure",
                image="unused",
                recovery_turns=0,
            ),
            backend=ClaudeAgentSDKBackend(
                client_factory=build_dry_run_client_factory(workspace_subdir="vulnerable_app"),
                enforce_subscription_environment=False,
            ),
            event_sink=sink,
            simulated_inference=True,
        )
    assert "sandbox_cleanup_attempted" in events
    assert "sandbox_cleanup_failed" in events
    assert "sandbox_closed" not in events
