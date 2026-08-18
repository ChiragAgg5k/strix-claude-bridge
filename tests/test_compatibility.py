from __future__ import annotations

import json
from pathlib import Path

import pytest

from strix_claude_bridge.backend import SessionCheckpoint, SessionCompatibilityError
from strix_claude_bridge.strix_integration import (
    SUPPORTED_CLAUDE_SDK_VERSION,
    SUPPORTED_OPENAI_AGENTS_VERSION,
    SUPPORTED_STRIX_COMMIT,
    SUPPORTED_STRIX_VERSION,
    IntegrationCompatibilityError,
    verify_runtime_compatibility,
    verify_strix_source,
)


def test_supported_versions_pass_and_version_drift_fails() -> None:
    verify_runtime_compatibility(
        strix_version=SUPPORTED_STRIX_VERSION,
        sdk_version=SUPPORTED_CLAUDE_SDK_VERSION,
        openai_agents_version=SUPPORTED_OPENAI_AGENTS_VERSION,
    )

    with pytest.raises(IntegrationCompatibilityError, match=r"strix-agent 1\.5\.4"):
        verify_runtime_compatibility(
            strix_version="1.5.4",
            sdk_version=SUPPORTED_CLAUDE_SDK_VERSION,
            openai_agents_version=SUPPORTED_OPENAI_AGENTS_VERSION,
        )


def test_pinned_strix_source_is_compatible() -> None:
    source = Path("/tmp/strix-study")
    if not source.is_dir():
        pytest.skip("pinned Strix study checkout is unavailable")

    verify_strix_source(source)


def test_source_commit_constant_is_full_git_hash() -> None:
    assert len(SUPPORTED_STRIX_COMMIT) == 40
    int(SUPPORTED_STRIX_COMMIT, 16)


def test_checkpoint_audit_redaction_and_compatibility_failure() -> None:
    checkpoint = SessionCheckpoint(
        provider_backend="claude-agent-sdk",
        backend_version="0.1.0",
        sdk_version=SUPPORTED_CLAUDE_SDK_VERSION,
        model="claude-sonnet",
        tool_schema_digest="abc",
        provider_session_id="opaque-session",
        cwd_identity="run-1",
        last_settled_turn=2,
    )

    audit = checkpoint.to_audit_dict()
    assert "provider_session_id" not in audit
    assert len(audit["provider_session_id_sha256"]) == 64
    assert "opaque-session" not in json.dumps(audit)
    checkpoint.require_compatible(
        provider_backend="claude-agent-sdk",
        backend_version="0.1.0",
        sdk_version=SUPPORTED_CLAUDE_SDK_VERSION,
        tool_schema_digest="abc",
        cwd_identity="run-1",
    )

    with pytest.raises(SessionCompatibilityError, match="tool_schema_digest"):
        checkpoint.require_compatible(
            provider_backend="claude-agent-sdk",
            backend_version="0.1.0",
            sdk_version=SUPPORTED_CLAUDE_SDK_VERSION,
            tool_schema_digest="changed",
            cwd_identity="run-1",
        )
