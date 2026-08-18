"""State, viewer, and event fanout for multi-agent bridge runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strix_claude_bridge.backend import BackendEvent, SessionCheckpoint
from strix_claude_bridge.runtime_state import RunStateStore, ToolInvocationJournal
from strix_claude_bridge.single_agent import BridgeEventSink
from strix_claude_bridge.viewer_state import ViewerStateStore


def build_multi_agent_run_artifacts(
    state_dir: Path,
    *,
    run_name: str,
    auth_mode: str,
    resume: bool,
    resume_token: str | None,
    report_state: Any,
    event_sink: BridgeEventSink | None,
) -> MultiAgentRunArtifacts:
    store = RunStateStore(state_dir / "claude-bridge")
    viewer_state = ViewerStateStore(state_dir)
    if resume:
        store.open_resume(str(resume_token))
    else:
        store.initialize(run_name=run_name, auth_mode=auth_mode)
    journal = ToolInvocationJournal(store.journal_path, replay_mode=resume)
    return MultiAgentRunArtifacts(
        store=store,
        viewer_state=viewer_state,
        journal=journal,
        report_state=report_state,
        event_sink=event_sink,
    )


@dataclass
class MultiAgentRunArtifacts:
    """Own the durable state, viewer mirror, and event fanout for one run."""

    store: RunStateStore
    viewer_state: ViewerStateStore
    journal: ToolInvocationJournal
    report_state: Any
    event_sink: BridgeEventSink | None = None

    @property
    def bridge_state_dir(self) -> Path:
        return self.store.state_dir

    async def emit(self, event: BackendEvent) -> None:
        self.store.append_event(event)
        await self.viewer_state.append_backend_event(event)
        if self.event_sink is not None:
            await self.event_sink(event)

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
        self.store.record_agent(
            agent_id=agent_id,
            name=name,
            parent_id=parent_id,
            task=task,
            skills=skills,
            status=status,
            checkpoint=checkpoint,
        )

    def checkpoint_for(self, agent_id: str) -> SessionCheckpoint | None:
        return self.store.checkpoint_for(agent_id)

    def record_usage(
        self,
        usage: dict[str, Any] | Any,
        *,
        model: str | None,
        observed_models: tuple[str, ...] = (),
    ) -> None:
        self.store.record_usage(usage, model=model, observed_models=observed_models)

    async def seed_agent(self, agent_id: str, initial_input: str) -> None:
        await self.viewer_state.seed_agent(agent_id, initial_input)

    def viewer_session_for(self, agent_id: str) -> Any:
        return self.viewer_state.session_for(agent_id)

    async def append_user_text(self, agent_id: str, text: str) -> None:
        await self.viewer_state.append_user_text(agent_id, text)

    def close(self) -> None:
        self.viewer_state.close()
