from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from strix_claude_bridge.auth import _AUTH_OVERRIDE_VARIABLES
from strix_claude_bridge.cli import build_parser
from strix_claude_bridge.strix_integration import (
    SUPPORTED_CLAUDE_SDK_VERSION,
    SUPPORTED_OPENAI_AGENTS_VERSION,
    SUPPORTED_STRIX_COMMIT,
    SUPPORTED_STRIX_VERSION,
)

ROOT = Path(__file__).parents[1]
REQUIRED_DOCS = {
    Path("README.md"),
    Path("COMPATIBILITY.md"),
    Path("docs/architecture.md"),
    Path("docs/ownership-boundaries.md"),
    Path("docs/implementation-status.md"),
    Path("docs/tool-parity.md"),
    Path("docs/tool-inventory.json"),
    Path("docs/events-sessions-resume.md"),
    Path("docs/security.md"),
    Path("docs/upstream-compatibility.md"),
    Path("docs/operations.md"),
    Path("docs/policy-gate.md"),
    Path("docs/decisions/README.md"),
    Path("docs/decisions/0001-integration-topology-packaging.md"),
    Path("docs/decisions/0002-execution-loop-ownership.md"),
    Path("docs/decisions/0003-tool-bridge.md"),
    Path("docs/decisions/0004-state-resume.md"),
    Path("docs/decisions/0005-event-translation.md"),
    Path("docs/decisions/0006-subscription-usage.md"),
}


def test_required_documentation_and_internal_links_exist() -> None:
    missing = sorted(str(path) for path in REQUIRED_DOCS if not (ROOT / path).is_file())
    assert missing == []
    for relative in sorted(REQUIRED_DOCS):
        if relative.suffix != ".md":
            continue
        source = ROOT / relative
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", source.read_text()):
            if target.startswith(("http://", "https://", "#")):
                continue
            path_text = target.split("#", 1)[0]
            target_path = (source.parent / path_text).resolve()
            if target_path.is_dir():
                target_path = target_path / "README.md"
            assert target_path.is_file(), f"broken link in {relative}: {target}"


def test_cli_help_and_readme_commands_are_consistent() -> None:
    parser = build_parser()
    scan_parser = next(action for action in parser._actions if action.dest == "operation").choices[
        "scan"
    ]
    help_text = scan_parser.format_help()
    assert "--resume-token" in help_text
    assert "disabled safety guard" in help_text
    readme = (ROOT / "README.md").read_text()
    for text in (
        "uv sync --extra test --extra strix --locked",
        "strix-claude-bridge scan",
        "--experimental --authorized-use",
        "--experimental --dry-run",
        "strix-claude-bridge sandbox-probe",
        "strix-claude-bridge live-probe --authorized-use",
        "--max-tool-calls-per-agent",
    ):
        assert text in readme
    scan_options = {option for action in scan_parser._actions for option in action.option_strings}
    for option in (
        "--experimental",
        "--authorized-use",
        "--target",
        "--scan-mode",
        "--run-name",
        "--max-turns",
        "--max-runtime",
        "--max-concurrent-agents",
        "--max-agents",
        "--max-tool-calls-per-agent",
    ):
        assert option in scan_options


def test_safe_live_commands_list_every_rejected_auth_override() -> None:
    for relative in ("README.md", "docs/operations.md"):
        text = (ROOT / relative).read_text()
        for variable in _AUTH_OVERRIDE_VARIABLES:
            assert variable in text, f"safe command in {relative} omits {variable}"


def test_version_pins_agree_across_code_metadata_docs_and_inventory() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    lock = (ROOT / "uv.lock").read_text()
    inventory = json.loads((ROOT / "docs/tool-inventory.json").read_text())["generated_from"]
    assert f'"strix-agent=={SUPPORTED_STRIX_VERSION}' in pyproject
    assert f'"openai-agents[litellm]=={SUPPORTED_OPENAI_AGENTS_VERSION}' in pyproject
    assert f'"claude-agent-sdk=={SUPPORTED_CLAUDE_SDK_VERSION}"' in pyproject
    assert f'version = "{SUPPORTED_STRIX_VERSION}"' in lock
    assert f'version = "{SUPPORTED_CLAUDE_SDK_VERSION}"' in lock
    assert inventory == {
        "claude_agent_sdk_version": SUPPORTED_CLAUDE_SDK_VERSION,
        "openai_agents_version": SUPPORTED_OPENAI_AGENTS_VERSION,
        "scan_mode": "quick",
        "skills": [],
        "strix_commit": SUPPORTED_STRIX_COMMIT,
        "strix_version": SUPPORTED_STRIX_VERSION,
        "target_type": "local_code",
    }
    for relative in (
        "COMPATIBILITY.md",
        "docs/architecture.md",
        "docs/upstream-compatibility.md",
    ):
        text = (ROOT / relative).read_text()
        for value in (
            SUPPORTED_STRIX_VERSION,
            SUPPORTED_OPENAI_AGENTS_VERSION,
            SUPPORTED_CLAUDE_SDK_VERSION,
            SUPPORTED_STRIX_COMMIT,
        ):
            assert value in text, f"missing pin {value} in {relative}"


def test_ownership_and_status_vocabularies_are_complete() -> None:
    ownership = (ROOT / "docs/ownership-boundaries.md").read_text()
    for category in (
        "Unchanged Strix",
        "Wrapped or Adapted Strix",
        "Bridge-owned",
        "Claude Agent SDK-owned",
        "Docker-owned",
        "User-policy-owned",
    ):
        assert category in ownership
    status = (ROOT / "docs/implementation-status.md").read_text()
    for value in ("Implemented", "Partial", "Unchanged Upstream", "Deferred", "Blocked"):
        assert value in status
    for phase in range(1, 10):
        assert f"Phase {phase}" in status
    for required_gap in (
        "native Strix",
        "restart",
        "TUI",
        "SQLite",
        "live",
        "organization",
    ):
        assert required_gap.casefold() in status.casefold()


def test_privacy_and_unconnected_viewer_status_match_implementation() -> None:
    events = (ROOT / "docs/events-sessions-resume.md").read_text()
    ownership = (ROOT / "docs/ownership-boundaries.md").read_text()
    security = (ROOT / "docs/security.md").read_text()
    assert "string `UserMessage`" in events
    assert "unknown SDK frame" in events
    assert "task SHA-256" in events
    assert "currently unconnected legacy projection helper" in ownership
    assert "raw provider session IDs stay in memory" in security


def test_committed_tool_inventory_matches_pinned_effective_strix_tools() -> None:
    subprocess.run(
        [sys.executable, "scripts/generate_tool_inventory.py", "--check"],
        cwd=ROOT,
        check=True,
        timeout=60,
    )
    inventory = json.loads((ROOT / "docs/tool-inventory.json").read_text())
    tools = inventory["tools"]
    assert len([tool for tool in tools if "root" in tool["roles"]]) == 33
    assert len([tool for tool in tools if "child" in tool["roles"]]) == 33
    assert {tool["name"] for tool in tools if tool["roles"] == ["root"]} == {"finish_scan"}
    assert {tool["name"] for tool in tools if tool["roles"] == ["child"]} == {"agent_finish"}
    assert all(tool["schema"]["type"] == "object" for tool in tools)
    assert all(len(tool["schema_sha256"]) == 64 for tool in tools)


def test_documented_bridge_source_references_exist() -> None:
    for relative in (
        "src/strix_claude_bridge/auth.py",
        "src/strix_claude_bridge/backend.py",
        "src/strix_claude_bridge/claude_backend.py",
        "src/strix_claude_bridge/cli.py",
        "src/strix_claude_bridge/event_adapter.py",
        "src/strix_claude_bridge/multi_agent.py",
        "src/strix_claude_bridge/runtime_state.py",
        "src/strix_claude_bridge/single_agent.py",
        "src/strix_claude_bridge/strix_integration.py",
        "src/strix_claude_bridge/tool_adapter.py",
        "scripts/generate_tool_inventory.py",
    ):
        assert (ROOT / relative).is_file()
