from __future__ import annotations

import json
from pathlib import Path

import pytest

from strix_claude_bridge.backend import (
    BackendEvent,
    SessionCheckpoint,
    SessionCompatibilityError,
)
from strix_claude_bridge.runtime_state import RunStateStore, ToolInvocationJournal


def test_restart_state_has_no_capability_and_resume_fails_closed(tmp_path: Path) -> None:
    state = RunStateStore(tmp_path / "state")
    state.initialize(run_name="run")
    journal = ToolInvocationJournal(state.journal_path)
    journal.begin("root", "exec_command", '{"cmd":"touch /tmp/x"}')

    metadata = json.loads(state.metadata_path.read_text())
    assert metadata == {"schema_version": 1, "run_name": "run", "agents": {}}
    task_secret = "TASK-CREDENTIAL-IN-SESSIONS-sentinel"
    provider_secret = "PROVIDER-SESSION-AUTHORITY-sentinel"
    state.record_agent(
        agent_id="root",
        name="Root Agent",
        parent_id=None,
        task=task_secret,
        skills=[],
        status="running",
        checkpoint=SessionCheckpoint(
            provider_backend="claude-agent-sdk",
            backend_version="0.1.0",
            sdk_version="0.2.139",
            model=None,
            tool_schema_digest="digest",
            provider_session_id=provider_secret,
            cwd_identity="cwd",
            last_settled_turn=1,
        ),
    )
    persisted = state.metadata_path.read_text()
    agent = json.loads(persisted)["agents"]["root"]
    assert task_secret not in persisted
    assert provider_secret not in persisted
    assert "task" not in agent
    assert len(agent["task_sha256"]) == 64
    assert "provider_session_id" not in agent["checkpoint"]
    assert len(agent["checkpoint"]["provider_session_id_sha256"]) == 64
    with pytest.raises(SessionCompatibilityError, match="process-restart resume is disabled"):
        RunStateStore(tmp_path / "state").open_resume("any-value")


def test_journal_persists_only_hashes_and_state_not_tool_content(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    arguments_secret = "ARGUMENT-CREDENTIAL-sentinel"
    output_secret = "OUTPUT-AND-MAILBOX-sentinel"
    journal = ToolInvocationJournal(path)
    first = journal.begin(
        "child",
        "send_message_to_agent",
        json.dumps({"message": output_secret, "request_body": arguments_secret}),
    )
    assert first.action == "execute"
    assert journal.is_agent_clean("child") is False
    assert journal.is_agent_clean("root") is True
    journal.complete(
        first.invocation_id or "",
        {
            "content": [{"type": "text", "text": output_secret}],
            "is_error": False,
        },
    )

    assert journal.is_agent_clean("child") is True
    persisted = path.read_text()
    entry = json.loads(persisted)["entries"][0]
    assert arguments_secret not in persisted
    assert output_secret not in persisted
    assert "content" not in entry
    assert "result" not in entry
    assert set(entry) == {
        "agent_id",
        "fingerprint",
        "invocation_id",
        "is_error",
        "result_sha256",
        "state",
        "tool_name",
    }
    assert entry["state"] == "completed"
    assert len(entry["fingerprint"]) == len(entry["result_sha256"]) == 64
    with pytest.raises(SessionCompatibilityError, match="automatic tool replay is disabled"):
        ToolInvocationJournal(path, replay_mode=True).begin(
            "child", "send_message_to_agent", '{"message":"new"}'
        )


def test_usage_and_transcript_have_subscription_zero_cost_semantics(tmp_path: Path) -> None:
    state = RunStateStore(tmp_path / "state")
    state.initialize(run_name="run")
    event = BackendEvent(
        "assistant_text", "root", {"text": "sensitive"}, sequence=1, sensitive=True
    )
    assert state.append_event(event) is True
    assert state.append_event(event) is False
    state.record_usage(
        {
            "input_tokens": 4,
            "output_tokens": 3,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 1,
            "total_cost_usd": 99,
        },
        model="claude-sonnet",
    )

    transcript = json.loads(state.transcript_path.read_text().strip())
    usage = json.loads(state.usage_path.read_text())
    assert transcript["payload"] == {"omitted": True}
    assert usage == {
        "schema_version": 1,
        "auth_mode": "claude_subscription",
        "requests": 1,
        "input_tokens": 4,
        "output_tokens": 3,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 1,
        "models": ["claude-sonnet"],
        "cost_usd": 0,
    }


def test_provider_models_override_selector_and_selector_is_only_a_fallback(tmp_path: Path) -> None:
    state = RunStateStore(tmp_path / "state")
    state.initialize(run_name="run")
    state.record_usage(
        {"input_tokens": 1, "output_tokens": 2},
        model="configured-selector",
        observed_models=("provider-model-id",),
    )
    state.record_usage(
        {"input_tokens": 3, "output_tokens": 4},
        model="configured-selector",
    )

    usage = json.loads(state.usage_path.read_text())
    assert usage["models"] == ["configured-selector", "provider-model-id"]
