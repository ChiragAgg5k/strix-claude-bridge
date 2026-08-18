"""Durable Claude session, mailbox, transcript, usage, and tool replay state."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from strix_claude_bridge.backend import BackendEvent, SessionCheckpoint, SessionCompatibilityError

_STATE_SCHEMA = 1


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def secure_run_tree(run_dir: Path) -> None:
    """Keep all run directories/files owner-only without relying on caller umask."""
    if not run_dir.exists():
        return
    for root, directories, files in os.walk(run_dir, followlinks=False):
        root_path = Path(root)
        if not root_path.is_symlink():
            os.chmod(root_path, 0o700)
        for name in directories:
            path = root_path / name
            if not path.is_symlink():
                os.chmod(path, 0o700)
        for name in files:
            path = root_path / name
            if not path.is_symlink():
                os.chmod(path, 0o600)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionCompatibilityError(f"malformed bridge state: {path.name}") from exc
    if not isinstance(value, dict):
        raise SessionCompatibilityError(f"malformed bridge state: {path.name}")
    return value


def cwd_identity(path: Path) -> str:
    """Return a stable non-secret identity for an agent SDK working directory."""
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()


@dataclass(frozen=True)
class JournalDecision:
    action: str
    invocation_id: str | None = None


@dataclass
class ToolInvocationJournal:
    """Metadata-only write-ahead ledger; tool arguments/results are never persisted."""

    path: Path
    replay_mode: bool = False
    _entries: list[dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.path.exists():
            data = _read_json(self.path)
            entries = data.get("entries")
            if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
                raise SessionCompatibilityError("malformed tool journal")
            self._entries = [dict(item) for item in entries]

    def _save(self) -> None:
        _atomic_json(self.path, {"schema_version": _STATE_SCHEMA, "entries": self._entries})

    @staticmethod
    def _fingerprint(agent_id: str, tool_name: str, raw_arguments: str) -> str:
        value = f"{agent_id}\0{tool_name}\0{raw_arguments}"
        return hashlib.sha256(value.encode()).hexdigest()

    @property
    def is_clean(self) -> bool:
        return not any(item.get("state") == "started" for item in self._entries)

    def is_agent_clean(self, agent_id: str) -> bool:
        """Return whether one agent has no indeterminate tool invocation."""
        return not any(
            item.get("agent_id") == agent_id and item.get("state") == "started"
            for item in self._entries
        )

    def require_resumable(self) -> None:
        ambiguous = [
            str(item.get("invocation_id") or "unknown")
            for item in self._entries
            if item.get("state") == "started"
        ]
        if ambiguous:
            raise SessionCompatibilityError(
                "resume blocked by indeterminate tool invocation(s): " + ", ".join(ambiguous)
            )

    def begin(self, agent_id: str, tool_name: str, raw_arguments: str) -> JournalDecision:
        fingerprint = self._fingerprint(agent_id, tool_name, raw_arguments)
        if self.replay_mode:
            raise SessionCompatibilityError(
                "automatic tool replay is disabled; reconcile the metadata-only journal manually"
            )
        invocation_id = f"tool-{len(self._entries) + 1:08d}"
        self._entries.append(
            {
                "invocation_id": invocation_id,
                "agent_id": agent_id,
                "tool_name": tool_name,
                "fingerprint": fingerprint,
                "state": "started",
            }
        )
        self._save()
        return JournalDecision("execute", invocation_id)

    def complete(self, invocation_id: str, result: Mapping[str, Any]) -> None:
        entry = next(
            (
                item
                for item in reversed(self._entries)
                if item.get("invocation_id") == invocation_id
            ),
            None,
        )
        if entry is None or entry.get("state") != "started":
            raise RuntimeError("tool journal completion does not match a started invocation")
        encoded = json.dumps(
            dict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        entry["state"] = "completed"
        entry["result_sha256"] = hashlib.sha256(encoded).hexdigest()
        entry["is_error"] = bool(result.get("is_error", False))
        self._save()

    def fail(self, invocation_id: str) -> None:
        entry = next(
            (
                item
                for item in reversed(self._entries)
                if item.get("invocation_id") == invocation_id
            ),
            None,
        )
        if entry is not None and entry.get("state") == "started":
            entry["state"] = "failed"
            self._save()


class RunStateStore:
    """Owner-only bridge metadata store; Claude native sessions remain inference authority."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.metadata_path = state_dir / "claude-sessions.json"
        self.transcript_path = state_dir / "claude-events.jsonl"
        self.usage_path = state_dir / "claude-usage.json"
        self.journal_path = state_dir / "tool-journal.json"
        self._metadata: dict[str, Any] = {}
        self._seen_events: set[str] = set()

    def initialize(
        self,
        *,
        run_name: str,
        auth_mode: str = "claude_subscription",
    ) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        self._metadata = {
            "schema_version": _STATE_SCHEMA,
            "run_name": run_name,
            "agents": {},
        }
        _atomic_json(self.metadata_path, self._metadata)
        _atomic_json(
            self.usage_path,
            {
                "schema_version": _STATE_SCHEMA,
                "auth_mode": auth_mode,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "models": [],
                "cost_usd": 0,
            },
        )

    def open_resume(self, _token: str, *, context_digest: str | None = None) -> None:
        del context_digest
        raise SessionCompatibilityError(
            "process-restart resume is disabled; start a fresh uniquely named run"
        )

    @property
    def agents(self) -> Mapping[str, Any]:
        return self._metadata.get("agents", {})

    def record_agent(
        self,
        *,
        agent_id: str,
        name: str,
        parent_id: str | None,
        task: str,
        skills: list[str],
        status: str,
        checkpoint: SessionCheckpoint | None = None,
    ) -> None:
        agents = self._metadata.setdefault("agents", {})
        prior = dict(agents.get(agent_id) or {})
        prior.pop("task", None)
        prior.update(
            {
                "name": name,
                "parent_id": parent_id,
                "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
                "skills": list(skills),
                "status": status,
            }
        )
        if checkpoint is not None:
            prior["checkpoint"] = checkpoint.to_audit_dict()
        agents[agent_id] = prior
        _atomic_json(self.metadata_path, self._metadata)

    def checkpoint_for(self, agent_id: str) -> SessionCheckpoint | None:
        value = (self._metadata.get("agents", {}).get(agent_id) or {}).get("checkpoint")
        if value is None:
            return None
        if "provider_session_id" not in value:
            return None
        try:
            return SessionCheckpoint(**value)
        except (TypeError, ValueError) as exc:
            raise SessionCompatibilityError(f"malformed checkpoint for agent {agent_id}") from exc

    def append_event(self, event: BackendEvent, *, include_sensitive: bool = False) -> bool:
        payload = (
            dict(event.payload) if include_sensitive or not event.sensitive else {"omitted": True}
        )
        value = {
            "schema_version": event.schema_version,
            "sequence": event.sequence,
            "kind": event.kind,
            "agent_id": event.agent_id,
            "sensitive": event.sensitive,
            "payload": payload,
        }
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if digest in self._seen_events:
            return False
        self._seen_events.add(digest)
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(self.transcript_path, 0o600)
        return True

    def record_usage(
        self,
        usage: Mapping[str, Any],
        *,
        model: str | None,
        observed_models: Sequence[str] = (),
    ) -> None:
        value = _read_json(self.usage_path)
        value["requests"] = int(value.get("requests") or 0) + 1
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value[key] = int(value.get(key) or 0) + int(usage.get(key) or 0)
        models = set(value.get("models") or [])
        reported = {item for item in observed_models if isinstance(item, str) and item}
        if reported:
            models.update(reported)
        elif model:
            models.add(model)
        value["models"] = sorted(models)
        if value.get("auth_mode") not in {"claude_subscription", "simulated_no_auth"}:
            raise SessionCompatibilityError("unsupported persisted authentication mode")
        value["cost_usd"] = 0
        _atomic_json(self.usage_path, value)
