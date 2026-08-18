from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo, ResultMessage
from mcp.types import CallToolRequest, CallToolRequestParams

from strix_claude_bridge.auth import AuthenticationModeError
from strix_claude_bridge.claude_backend import ClaudeAgentSDKBackend
from strix_claude_bridge.multi_agent import (
    MultiAgentScanConfig,
    _DurableCoordinatorMixin,
    run_multi_agent_scan,
)
from strix_claude_bridge.multi_agent_dry_run import build_multi_agent_dry_run_client_factory
from strix_claude_bridge.single_agent import SingleAgentScanError
from strix_claude_bridge.strix_integration import StrixAgentBridgeInput, build_claude_session_spec


@pytest.mark.asyncio
async def test_direct_live_api_rejects_auth_override_before_runner_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from strix_claude_bridge import auth, multi_agent

    for variable in auth._AUTH_OVERRIDE_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    secret = "sk-ant-direct-api-preflight-sentinel"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    constructed = False

    class UnexpectedRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True
            raise AssertionError("runner construction must follow authentication preflight")

    monkeypatch.setattr(multi_agent, "MultiAgentScanRunner", UnexpectedRunner)
    config = MultiAgentScanConfig(
        scan_config={"targets": []},
        run_name="auth-preflight",
        image="example.invalid/strix:test",
    )

    with pytest.raises(AuthenticationModeError) as exc_info:
        await run_multi_agent_scan(config)

    assert constructed is False
    assert secret not in str(exc_info.value)


@pytest.mark.asyncio
async def test_mailbox_is_snapshotted_before_active_session_interrupt(tmp_path: Path) -> None:
    from strix.core.agents import AgentCoordinator

    Coordinator = type("TestCoordinator", (_DurableCoordinatorMixin, AgentCoordinator), {})
    coordinator = Coordinator()
    snapshot = tmp_path / "graph.json"
    coordinator.set_snapshot_path(snapshot)
    task_secret = "TASK-CREDENTIAL-IN-GRAPH-sentinel"
    await coordinator.register("root", "Root", None, task=task_secret)

    class Stream:
        def cancel(self, *, mode: str) -> None:
            assert mode == "immediate"
            persisted = snapshot.read_text()
            data = json.loads(persisted)
            message = data["mailboxes"]["root"][0]
            assert message["id"] == "message-1"
            assert len(message["content_sha256"]) == 64
            assert "MAILBOX-SECRET-sentinel" not in persisted
            assert task_secret not in persisted
            assert "content" not in message
            assert "task" not in data["metadata"]["root"]
            assert len(data["metadata"]["root"]["task_sha256"]) == 64

    await coordinator.attach_stream("root", Stream())
    await coordinator.attach_runtime("root", interrupt_on_message=True)
    assert await coordinator.send(
        "root",
        {
            "id": "message-1",
            "from": "child",
            "content": "MAILBOX-SECRET-sentinel",
        },
    )


@pytest.mark.asyncio
async def test_send_and_stop_agent_tools_cross_real_mcp_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from strix.core.agents import AgentCoordinator
    from strix.tools.agents_graph.tools import send_message_to_agent, stop_agent

    from strix_claude_bridge import strix_integration

    monkeypatch.setattr(strix_integration, "verify_runtime_compatibility", lambda: None)
    coordinator = AgentCoordinator()
    await coordinator.register("root", "Root", None)
    await coordinator.register("child", "Child", "root")
    spec = build_claude_session_spec(
        StrixAgentBridgeInput(
            agent_id="root",
            system_prompt="authorized",
            cwd=tmp_path,
            context={"agent_id": "root", "coordinator": coordinator},
            function_tools=[send_message_to_agent, stop_agent],
        )
    )
    instance = spec.mcp_servers["strix_root"].configuration["instance"]

    send = await instance.request_handlers[CallToolRequest](
        CallToolRequest(
            params=CallToolRequestParams(
                name="send_message_to_agent",
                arguments={
                    "target_agent_id": "child",
                    "message": "wrap up",
                    "message_type": "instruction",
                    "priority": "normal",
                },
            )
        )
    )
    stop = await instance.request_handlers[CallToolRequest](
        CallToolRequest(
            params=CallToolRequestParams(
                name="stop_agent",
                arguments={"target_agent_id": "child", "cascade": True, "reason": "test"},
            )
        )
    )

    assert send.root.isError is False
    assert '"success": true' in send.root.content[0].text
    assert stop.root.isError is False
    assert coordinator.statuses["child"] == "stopped"


class FakeSandboxSession:
    def supports_pty(self) -> bool:
        return False

    async def exec(self, *_args: object, **_kwargs: object) -> Any:
        from agents.sandbox.types import ExecResult

        expected_failure = bool(_args and "false" in str(_args[0]))
        return ExecResult(
            stdout=(
                b""
                if expected_failure
                else b"9: # STRIX_DRY_RUN_PATH_TRAVERSAL: fixture evidence\n"
            ),
            stderr=b"expected command failure" if expected_failure else b"",
            exit_code=1 if expected_failure else 0,
        )

    async def write(self, _path: Path, _data: object) -> None:
        return


class FakeClient:
    def __init__(self) -> None:
        self.deleted: list[Any] = []

    async def delete(self, session: Any) -> None:
        self.deleted.append(session)


class FakeCaido:
    async def aclose(self) -> None:
        return


def _scan_config(fixture: Path) -> dict[str, Any]:
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


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    from strix.runtime import session_manager

    from strix_claude_bridge import multi_agent, strix_integration

    async def create_or_reuse(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {
            "session": FakeSandboxSession(),
            "client": client,
            "caido_client": FakeCaido(),
        }

    monkeypatch.setattr(session_manager, "create_or_reuse", create_or_reuse)
    monkeypatch.setattr(multi_agent, "verify_runtime_compatibility", lambda: None)
    monkeypatch.setattr(strix_integration, "verify_runtime_compatibility", lambda: None)


@pytest.mark.asyncio
async def test_simulated_root_child_scan_is_concurrent_and_persists_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)
    client_class = build_multi_agent_dry_run_client_factory(workspace_subdir="vulnerable_app")

    outcome = await run_multi_agent_scan(
        MultiAgentScanConfig(
            scan_config=_scan_config(fixture),
            run_name="multi-agent",
            image="unused",
            max_turns=10,
            max_runtime_s=30,
            max_concurrent_agents=2,
        ),
        backend=ClaudeAgentSDKBackend(
            client_factory=client_class,
            enforce_subscription_environment=False,
        ),
        simulated_inference=True,
    )

    run_dir = tmp_path / "strix_runs" / "multi-agent"
    sessions = json.loads(
        (run_dir / ".state" / "claude-bridge" / "claude-sessions.json").read_text()
    )
    usage = json.loads((run_dir / ".state" / "claude-bridge" / "claude-usage.json").read_text())
    run_record = json.loads((run_dir / "run.json").read_text())
    assert outcome.completed is True
    assert outcome.vulnerability_count == 1
    assert client_class.peak_active == 2
    assert len(client_class.sessions) == 2
    assert {value["status"] for value in sessions["agents"].values()} == {"completed"}
    assert all(
        value.get("checkpoint", {}).get("provider_session_id_sha256")
        for value in sessions["agents"].values()
    )
    assert all(
        "provider_session_id" not in value.get("checkpoint", {})
        for value in sessions["agents"].values()
    )
    assert all(
        "task" not in value and value.get("task_sha256") for value in sessions["agents"].values()
    )
    assert usage["cost_usd"] == 0
    assert usage["requests"] == 2
    assert usage["models"] == []
    agents = {item["agent_id"]: item for item in run_record["llm_usage"]["agents"]}
    assert agents["root0001"]["agent_name"] == "Root Agent"
    assert agents["agent002"]["agent_name"] == "Fixture Specialist"
    assert all(item["model"] is None for item in agents.values())
    assert (run_dir / "findings.sarif").is_file()
    assert (run_dir / "penetration_test_report.md").is_file()
    assert run_dir.stat().st_mode & 0o777 == 0o700
    assert (run_dir / "findings.sarif").stat().st_mode & 0o777 == 0o600
    transcript = (run_dir / ".state" / "claude-bridge" / "claude-events.jsonl").read_text()
    assert "resume_capability" not in transcript
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_allowed_rate_limit_event_does_not_abort_live_style_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)
    base_client = build_multi_agent_dry_run_client_factory(workspace_subdir="vulnerable_app")

    class AllowedRateEventClient(base_client):
        async def receive_response(self):
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed",
                    rate_limit_type="five_hour",
                    overage_status="rejected",
                    overage_disabled_reason="out_of_credits",
                ),
                uuid="provider-event-secret",
                session_id="provider-session-secret",
            )
            async for message in super().receive_response():
                yield message

    outcome = await run_multi_agent_scan(
        MultiAgentScanConfig(
            scan_config=_scan_config(fixture),
            run_name="allowed-rate-event",
            image="unused",
            max_turns=10,
            max_runtime_s=30,
            max_concurrent_agents=2,
        ),
        backend=ClaudeAgentSDKBackend(
            client_factory=AllowedRateEventClient,
            enforce_subscription_environment=False,
        ),
        simulated_inference=True,
    )

    transcript = (
        tmp_path
        / "strix_runs"
        / "allowed-rate-event"
        / ".state"
        / "claude-bridge"
        / "claude-events.jsonl"
    ).read_text()
    assert outcome.completed is True
    assert '"status": "allowed"' in transcript
    assert "provider-event-secret" not in transcript
    assert "provider-session-secret" not in transcript
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_rejected_rate_limit_event_aborts_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)
    base_client = build_multi_agent_dry_run_client_factory(workspace_subdir="vulnerable_app")

    class RejectedRateEventClient(base_client):
        async def receive_response(self):
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(status="rejected", rate_limit_type="five_hour"),
                uuid="provider-event-secret",
                session_id="provider-session-secret",
            )
            async for message in super().receive_response():
                yield message

    with pytest.raises(SingleAgentScanError, match="rate or plan limit"):
        await run_multi_agent_scan(
            MultiAgentScanConfig(
                scan_config=_scan_config(fixture),
                run_name="rejected-rate-event",
                image="unused",
                max_turns=10,
                max_runtime_s=30,
                max_concurrent_agents=2,
            ),
            backend=ClaudeAgentSDKBackend(
                client_factory=RejectedRateEventClient,
                enforce_subscription_environment=False,
            ),
            simulated_inference=True,
        )
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_root_recovers_only_from_proven_mailbox_interrupted_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)
    base_client = build_multi_agent_dry_run_client_factory(workspace_subdir="vulnerable_app")

    class InterruptedWaitClient(base_client):
        interrupt_calls = 0

        async def interrupt(self) -> None:
            type(self).interrupt_calls += 1

        async def receive_response(self):
            if self.is_root and self.options.resume is None:
                create = {
                    "name": "Fixture Specialist",
                    "task": "Inspect the authorized vulnerable fixture and report one finding.",
                    "inherit_context": False,
                    "skills": [],
                }
                async for message in self._tool("create_agent", create, "root-create"):
                    yield message
                async for message in self._tool(
                    "wait_for_agents",
                    {"reason": "Wait for fixture specialist", "timeout_seconds": 30},
                    "root-wait",
                ):
                    yield message
                yield ResultMessage(
                    subtype="error",
                    duration_ms=1,
                    duration_api_ms=0,
                    is_error=True,
                    num_turns=1,
                    session_id=self.session_id,
                    usage={"input_tokens": 1, "output_tokens": 1},
                    terminal_reason="aborted_tools",
                )
                return
            if self.is_root:
                finish = {
                    "executive_summary": "One path traversal was confirmed by a child agent.",
                    "methodology": "Delegated inspection through the Strix coordinator.",
                    "technical_analysis": "The child verified and filed the issue.",
                    "recommendations": "Confine resolved paths beneath the data root.",
                }
                async for message in self._tool("finish_scan", finish, "root-finish"):
                    yield message
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id=self.session_id,
                    usage={"input_tokens": 1, "output_tokens": 1},
                    terminal_reason="completed",
                )
                return
            async for message in super().receive_response():
                yield message

    outcome = await run_multi_agent_scan(
        MultiAgentScanConfig(
            scan_config=_scan_config(fixture),
            run_name="aborted-tools-recovery",
            image="unused",
            max_turns=10,
            max_runtime_s=30,
            max_concurrent_agents=2,
        ),
        backend=ClaudeAgentSDKBackend(
            client_factory=InterruptedWaitClient,
            enforce_subscription_environment=False,
        ),
        simulated_inference=True,
    )

    assert outcome.completed is True
    assert outcome.vulnerability_count == 1
    assert InterruptedWaitClient.interrupt_calls == 1
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_unproven_aborted_tools_result_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)

    class UnprovenAbortedClient:
        def __init__(self, _options: Any) -> None:
            return

        async def connect(self) -> None:
            return

        async def query(self, _prompt: str) -> None:
            return

        async def receive_response(self):
            yield ResultMessage(
                subtype="error",
                duration_ms=1,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="unproven-abort-session",
                usage={"input_tokens": 1, "output_tokens": 0},
                terminal_reason="aborted_tools",
            )

        async def interrupt(self) -> None:
            return

        async def disconnect(self) -> None:
            return

    with pytest.raises(SingleAgentScanError, match="aborted_tools"):
        await run_multi_agent_scan(
            MultiAgentScanConfig(
                scan_config=_scan_config(fixture),
                run_name="unproven-aborted-tools",
                image="unused",
                max_runtime_s=10,
            ),
            backend=ClaudeAgentSDKBackend(
                client_factory=UnprovenAbortedClient,
                enforce_subscription_environment=False,
            ),
            simulated_inference=True,
        )
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_concurrency_one_parks_root_wait_so_child_can_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)
    client_class = build_multi_agent_dry_run_client_factory(workspace_subdir="vulnerable_app")

    outcome = await run_multi_agent_scan(
        MultiAgentScanConfig(
            scan_config=_scan_config(fixture),
            run_name="concurrency-one",
            image="unused",
            max_turns=10,
            max_runtime_s=10,
            max_concurrent_agents=1,
        ),
        backend=ClaudeAgentSDKBackend(
            client_factory=client_class,
            enforce_subscription_environment=False,
        ),
        simulated_inference=True,
    )

    assert outcome.completed is True
    # Root response remains transport-active while its coordinator wait is parked;
    # the released inference permit lets the child make progress.
    assert client_class.peak_active == 2
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_scan_recovers_after_ordinary_tool_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)
    base = build_multi_agent_dry_run_client_factory(workspace_subdir="vulnerable_app")

    class RecoveringClient(base):
        async def receive_response(self):
            if not self.is_root:
                async for item in self._tool(
                    "exec_command",
                    {"cmd": "false", "yield_time_ms": 10000},
                    "expected-command-failure",
                ):
                    yield item
            async for item in super().receive_response():
                yield item

    outcome = await run_multi_agent_scan(
        MultiAgentScanConfig(
            scan_config=_scan_config(fixture),
            run_name="tool-recovery",
            image="unused",
            max_turns=10,
            max_runtime_s=10,
            max_concurrent_agents=1,
        ),
        backend=ClaudeAgentSDKBackend(
            client_factory=RecoveringClient,
            enforce_subscription_environment=False,
        ),
        simulated_inference=True,
    )

    journal = json.loads(
        (
            tmp_path
            / "strix_runs"
            / "tool-recovery"
            / ".state"
            / "claude-bridge"
            / "tool-journal.json"
        ).read_text()
    )
    assert outcome.completed is True
    assert len([item for item in journal["entries"] if item["tool_name"] == "exec_command"]) == 2
    assert len(sandbox_client.deleted) == 1


def test_process_restart_resume_is_disabled() -> None:
    with pytest.raises(ValueError, match="process-restart resume is disabled"):
        MultiAgentScanConfig(
            scan_config={}, run_name="resume", image="unused", resume_token="authority"
        )


@pytest.mark.asyncio
async def test_multi_agent_scan_cancellation_settles_session_and_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)

    class HangingClient:
        connected = asyncio.Event()
        interrupted = False
        disconnected = False

        def __init__(self, _options: Any) -> None:
            return

        async def connect(self) -> None:
            self.connected.set()

        async def query(self, _prompt: str) -> None:
            return

        async def receive_response(self):
            await asyncio.Event().wait()
            if False:
                yield None

        async def interrupt(self) -> None:
            type(self).interrupted = True

        async def disconnect(self) -> None:
            type(self).disconnected = True

    task = asyncio.create_task(
        run_multi_agent_scan(
            MultiAgentScanConfig(
                scan_config=_scan_config(fixture),
                run_name="multi-cancel",
                image="unused",
                max_runtime_s=30,
                max_concurrent_agents=2,
            ),
            backend=ClaudeAgentSDKBackend(
                client_factory=HangingClient,
                interrupt_grace_s=0.01,
                enforce_subscription_environment=False,
            ),
            simulated_inference=True,
        )
    )
    await asyncio.wait_for(HangingClient.connected.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = json.loads((tmp_path / "strix_runs" / "multi-cancel" / "run.json").read_text())
    assert record["status"] == "interrupted"
    assert HangingClient.interrupted is True
    assert HangingClient.disconnected is True
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_max_runtime_interrupts_active_root_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)

    class SlowClient:
        def __init__(self, _options: Any) -> None:
            return

        async def connect(self) -> None:
            return

        async def query(self, _prompt: str) -> None:
            return

        async def receive_response(self):
            await asyncio.Event().wait()
            if False:
                yield None

        async def interrupt(self) -> None:
            return

        async def disconnect(self) -> None:
            return

    with pytest.raises(SingleAgentScanError, match="exceeded --max-runtime"):
        await run_multi_agent_scan(
            MultiAgentScanConfig(
                scan_config=_scan_config(fixture),
                run_name="runtime-limit",
                image="unused",
                max_runtime_s=0.01,
            ),
            backend=ClaudeAgentSDKBackend(
                client_factory=SlowClient,
                interrupt_grace_s=0.01,
                enforce_subscription_environment=False,
            ),
            simulated_inference=True,
        )
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_root_rate_limit_has_actionable_error_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)

    class RateLimitedClient:
        def __init__(self, _options: Any) -> None:
            return

        async def connect(self) -> None:
            return

        async def query(self, _prompt: str) -> None:
            return

        async def receive_response(self):
            yield ResultMessage(
                subtype="error",
                duration_ms=1,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="settled-rate-session",
                usage={"input_tokens": 1, "output_tokens": 0},
                terminal_reason="rate_limit",
            )

        async def interrupt(self) -> None:
            return

        async def disconnect(self) -> None:
            return

    with pytest.raises(SingleAgentScanError, match="rate or plan limit"):
        await run_multi_agent_scan(
            MultiAgentScanConfig(
                scan_config=_scan_config(fixture),
                run_name="root-rate-limit",
                image="unused",
                max_runtime_s=10,
            ),
            backend=ClaudeAgentSDKBackend(
                client_factory=RateLimitedClient,
                enforce_subscription_environment=False,
            ),
            simulated_inference=True,
        )
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_context_window_pressure_fails_cleanly_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)

    class ContextWindowClient:
        def __init__(self, _options: Any) -> None:
            return

        async def connect(self) -> None:
            return

        async def query(self, _prompt: str) -> None:
            return

        async def receive_response(self):
            yield ResultMessage(
                subtype="error",
                duration_ms=1,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id="settled-context-session",
                usage={"input_tokens": 1, "output_tokens": 0},
                terminal_reason="context_window_exceeded",
            )

        async def interrupt(self) -> None:
            return

        async def disconnect(self) -> None:
            return

    with pytest.raises(SingleAgentScanError, match="context_window_exceeded"):
        await run_multi_agent_scan(
            MultiAgentScanConfig(
                scan_config=_scan_config(fixture),
                run_name="context-window",
                image="unused",
                max_runtime_s=10,
            ),
            backend=ClaudeAgentSDKBackend(
                client_factory=ContextWindowClient,
                enforce_subscription_environment=False,
            ),
            simulated_inference=True,
        )
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_role", ["root", "child"])
async def test_sdk_disconnect_failure_is_recorded_and_affects_agent_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_role: str
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)
    base = build_multi_agent_dry_run_client_factory(workspace_subdir="vulnerable_app")

    class DisconnectFailureClient(base):
        async def disconnect(self) -> None:
            await super().disconnect()
            if (fail_role == "root" and self.is_root) or (
                fail_role == "child" and not self.is_root
            ):
                raise RuntimeError("sanitized by runner")

    events: list[Any] = []
    config = MultiAgentScanConfig(
        scan_config=_scan_config(fixture),
        run_name=f"disconnect-{fail_role}",
        image="unused",
        max_turns=10,
        max_runtime_s=10,
        max_concurrent_agents=2,
    )
    backend = ClaudeAgentSDKBackend(
        client_factory=DisconnectFailureClient,
        enforce_subscription_environment=False,
    )
    if fail_role == "root":
        with pytest.raises(RuntimeError, match="sanitized by runner"):
            await run_multi_agent_scan(
                config,
                backend=backend,
                event_sink=lambda event: _append_event(events, event),
                simulated_inference=True,
            )
    else:
        await run_multi_agent_scan(
            config,
            backend=backend,
            event_sink=lambda event: _append_event(events, event),
            simulated_inference=True,
        )

    sessions = json.loads(
        (
            tmp_path
            / "strix_runs"
            / f"disconnect-{fail_role}"
            / ".state"
            / "claude-bridge"
            / "claude-sessions.json"
        ).read_text()
    )
    affected = [
        value
        for value in sessions["agents"].values()
        if (fail_role == "root" and value["parent_id"] is None)
        or (fail_role == "child" and value["parent_id"] is not None)
    ]
    assert affected[0]["status"] == "failed"
    assert any(event.kind == "sdk_cleanup_failed" for event in events)
    assert len(sandbox_client.deleted) == 1


async def _append_event(events: list[Any], event: Any) -> None:
    events.append(event)


@pytest.mark.asyncio
async def test_cancel_disconnect_failure_is_mirrored_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)
    events: list[Any] = []

    class HangingDisconnectFailure:
        connected = asyncio.Event()

        def __init__(self, _options: Any) -> None:
            return

        async def connect(self) -> None:
            self.connected.set()

        async def query(self, _prompt: str) -> None:
            return

        async def receive_response(self):
            await asyncio.Event().wait()
            if False:
                yield None

        async def interrupt(self) -> None:
            return

        async def disconnect(self) -> None:
            raise RuntimeError("private disconnect detail")

    task = asyncio.create_task(
        run_multi_agent_scan(
            MultiAgentScanConfig(
                scan_config=_scan_config(fixture),
                run_name="cancel-disconnect-failure",
                image="unused",
                max_runtime_s=30,
            ),
            backend=ClaudeAgentSDKBackend(
                client_factory=HangingDisconnectFailure,
                interrupt_grace_s=0.01,
                enforce_subscription_environment=False,
            ),
            event_sink=lambda event: _append_event(events, event),
            simulated_inference=True,
        )
    )
    await asyncio.wait_for(HangingDisconnectFailure.connected.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any(event.kind == "sdk_cleanup_failed" for event in events)
    assert len(sandbox_client.deleted) == 1


@pytest.mark.asyncio
async def test_failed_child_notifies_parent_once_and_does_not_leave_root_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    sandbox_client = FakeClient()
    _patch_runtime(monkeypatch, sandbox_client)
    base = build_multi_agent_dry_run_client_factory(workspace_subdir="vulnerable_app")

    class FailingChildClient(base):
        wait_results: ClassVar[list[str]] = []

        async def _call(self, name: str, arguments: dict[str, Any]) -> str:
            result = await super()._call(name, arguments)
            if name == "wait_for_agents":
                type(self).wait_results.append(result)
            return result

        async def receive_response(self):
            if self.is_root:
                async for item in super().receive_response():
                    yield item
                return
            yield ResultMessage(
                subtype="error",
                duration_ms=1,
                duration_api_ms=0,
                is_error=True,
                num_turns=1,
                session_id=self.session_id,
                usage={"input_tokens": 0, "output_tokens": 0},
                terminal_reason="rate_limit",
            )

    outcome = await run_multi_agent_scan(
        MultiAgentScanConfig(
            scan_config=_scan_config(fixture),
            run_name="child-failure",
            image="unused",
            max_turns=10,
            max_runtime_s=10,
            max_concurrent_agents=2,
        ),
        backend=ClaudeAgentSDKBackend(
            client_factory=FailingChildClient,
            enforce_subscription_environment=False,
        ),
        simulated_inference=True,
    )

    graph = json.loads(
        (
            tmp_path
            / "strix_runs"
            / "child-failure"
            / ".state"
            / "claude-bridge"
            / "agent-graph.json"
        ).read_text()
    )
    journal_text = (
        tmp_path / "strix_runs" / "child-failure" / ".state" / "claude-bridge" / "tool-journal.json"
    ).read_text()
    assert outcome.completed is True
    assert list(graph["statuses"].values()).count("failed") == 1
    assert graph["pending_counts"]["root0001"] == 0
    assert len(FailingChildClient.wait_results) == 1
    assert FailingChildClient.wait_results[0].count("[Agent failed]") == 1
    assert "[Agent failed]" not in journal_text
