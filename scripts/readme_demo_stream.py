#!/usr/bin/env python3
"""Render metadata-only bridge JSONL as a compact terminal demo."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[38;2;77;214;221m"
AMBER = "\033[38;2;247;186;74m"
GREEN = "\033[38;2;106;215;142m"
WHITE = "\033[38;2;226;232;235m"


def line(symbol: str, label: str, detail: str = "", *, color: str = WHITE) -> None:
    suffix = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"{color}{symbol} {label}{RESET}{suffix}", flush=True)


def payload(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("payload")
    return value if isinstance(value, dict) else {}


def render(row: dict[str, Any], call_counts: dict[str, int]) -> None:
    kind = row.get("kind")
    event = row.get("event_kind")
    agent = str(row.get("agent_id") or "")
    data = payload(row)

    if kind == "scan_started":
        mode = "credential-free demo" if data.get("simulated_inference") else "live inference"
        line("◆", "Strix scan started", f"Claude Agent SDK · {mode}", color=CYAN)
    elif event == "sandbox_ready":
        line("◆", "Strix sandbox ready", str(data.get("image", "Docker")), color=CYAN)
    elif event == "tool_call":
        call_counts[agent] = call_counts.get(agent, 0) + 1
        role = "root" if agent == "root0001" else "child"
        line("→", f"{role} agent tool call #{call_counts[agent]}", "content omitted", color=AMBER)
    elif event == "terminal":
        role = "Root agent" if agent == "root0001" else "Child agent"
        line("✓", f"{role} completed", f"turns={data.get('turns', 0)}", color=GREEN)
    elif event == "sandbox_closed":
        verified = "deletion verified" if data.get("deletion_verified") else "closed"
        line("✓", "Sandbox removed", verified, color=GREEN)
    elif kind == "scan_completed":
        line(
            "✓",
            "End-to-end scan complete",
            f"findings={data.get('vulnerability_count', 0)} · status={data.get('terminal_reason')}",
            color=GREEN,
        )
        run_dir = Path(str(data.get("run_dir", "")))
        artifacts = (
            "penetration_test_report.md",
            "findings.sarif",
            "vulnerabilities.json",
            "vulnerabilities.csv",
        )
        for name in artifacts:
            if (run_dir / name).is_file():
                line("  ↳", name, color=WHITE)


def main() -> int:
    call_counts: dict[str, int] = {}
    for raw_line in sys.stdin:
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            render(row, call_counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
