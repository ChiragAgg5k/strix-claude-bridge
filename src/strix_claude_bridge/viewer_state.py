"""Bridge-to-Strix viewer state mirroring for completed/local run inspection."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from strix.core.sessions import open_agent_session, seed_initial_input, session_write_lock

from strix_claude_bridge.backend import BackendEvent


class ViewerStateStore:
    """Mirror bridge activity into the native Strix viewer state files."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.agents_db_path = state_dir / "agents.db"
        self._sessions: dict[str, Any] = {}

    def session_for(self, agent_id: str) -> Any:
        session = self._sessions.get(agent_id)
        if session is None:
            session = open_agent_session(agent_id, self.agents_db_path)
            self._sessions[agent_id] = session
        return session

    async def seed_agent(self, agent_id: str, initial_input: str) -> None:
        if not initial_input.strip():
            return
        await seed_initial_input(self.session_for(agent_id), initial_input)

    async def append_user_text(self, agent_id: str, text: str) -> None:
        if not text.strip():
            return
        await self.append_item(agent_id, {"role": "user", "content": text})

    async def append_item(self, agent_id: str, item: dict[str, Any]) -> None:
        session = self.session_for(agent_id)
        async with session_write_lock(session):
            await session.add_items([item])

    async def append_backend_event(self, event: BackendEvent) -> None:
        item = _history_item_for_event(event)
        if item is None:
            return
        await self.append_item(event.agent_id, item)

    def close(self) -> None:
        for session in self._sessions.values():
            with suppress(Exception):
                session.close()


def _history_item_for_event(event: BackendEvent) -> dict[str, Any] | None:
    payload = dict(event.payload)
    if event.kind == "assistant_text":
        text = str(payload.get("text") or "")
        return {"role": "assistant", "content": text} if text else None
    if event.kind == "user_message":
        text = str(payload.get("text") or "")
        return {"role": "user", "content": text} if text else None
    if event.kind == "tool_call":
        call_id = str(payload.get("call_id") or "")
        name = str(payload.get("name") or "tool")
        return {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": _json_string(payload.get("arguments", {})),
        }
    if event.kind == "tool_result":
        call_id = str(payload.get("call_id") or "")
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": payload.get("content"),
        }
    return None


def _json_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
