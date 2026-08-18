"""Generate the committed effective-tool inventory from the pinned Strix runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from strix_claude_bridge.multi_agent import _effective_agent_and_tools
from strix_claude_bridge.strix_integration import (
    SUPPORTED_CLAUDE_SDK_VERSION,
    SUPPORTED_OPENAI_AGENTS_VERSION,
    SUPPORTED_STRIX_COMMIT,
    SUPPORTED_STRIX_VERSION,
    verify_runtime_compatibility,
)


class InventorySandbox:
    """Capability binding placeholder; inventory generation never invokes a tool."""

    def supports_pty(self) -> bool:
        return False


def _tools(*, is_root: bool) -> list[Any]:
    fixture = Path("fixtures/vulnerable_app").resolve()
    scan_config = {
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
        "scan_mode": "quick",
        "skills": [],
    }
    _, tools = _effective_agent_and_tools(
        sandbox_session=InventorySandbox(),
        scan_config=scan_config,
        name="Root Agent" if is_root else "Inventory Child",
        skills=[],
        is_root=is_root,
    )
    return tools


def generate_inventory() -> dict[str, Any]:
    verify_runtime_compatibility()
    by_role = {
        "root": {tool.name: tool for tool in _tools(is_root=True)},
        "child": {tool.name: tool for tool in _tools(is_root=False)},
    }
    entries = []
    for name in sorted(set().union(*(set(value) for value in by_role.values()))):
        roles = [role for role, tools in by_role.items() if name in tools]
        schemas = [dict(by_role[role][name].params_json_schema) for role in roles]
        if any(schema != schemas[0] for schema in schemas[1:]):
            raise RuntimeError(f"tool schema differs by role: {name}")
        schema = schemas[0]
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
        entries.append(
            {
                "name": name,
                "roles": roles,
                "mcp_names": {
                    role: f"mcp__strix_{'root0001' if role == 'root' else 'agent002'}__{name}"
                    for role in roles
                },
                "schema_sha256": hashlib.sha256(encoded).hexdigest(),
                "timeout_seconds": getattr(by_role[roles[0]][name], "timeout_seconds", None),
                "schema": schema,
            }
        )
    return {
        "schema_version": 1,
        "generated_from": {
            "strix_version": SUPPORTED_STRIX_VERSION,
            "strix_commit": SUPPORTED_STRIX_COMMIT,
            "openai_agents_version": SUPPORTED_OPENAI_AGENTS_VERSION,
            "claude_agent_sdk_version": SUPPORTED_CLAUDE_SDK_VERSION,
            "scan_mode": "quick",
            "target_type": "local_code",
            "skills": [],
        },
        "tools": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("docs/tool-inventory.json"))
    args = parser.parse_args()
    rendered = json.dumps(generate_inventory(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            raise SystemExit(f"stale tool inventory: run {Path(__file__)}")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
