from __future__ import annotations

import json
import os
from pathlib import Path

import docker
import pytest

from strix_claude_bridge.cli import main

pytestmark = pytest.mark.docker


def test_real_strix_sandbox_dry_run_creates_report_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if os.environ.get("STRIX_BRIDGE_RUN_DOCKER_TESTS") != "1":
        pytest.skip("set STRIX_BRIDGE_RUN_DOCKER_TESTS=1 to opt in")

    fixture = Path(__file__).parents[1] / "fixtures" / "vulnerable_app"
    monkeypatch.chdir(tmp_path)
    docker_client = docker.from_env()
    strix_filter = {"label": "org.strix.runtime"}
    before = {
        container["Id"]
        for container in docker_client.api.containers(all=True, filters=strix_filter)
    }
    exit_code = main(
        [
            "scan",
            "--dry-run",
            "--target",
            str(fixture),
            "--scan-mode",
            "quick",
            "--run-name",
            "real-strix-dry-run",
            "--max-turns",
            "4",
        ]
    )

    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert exit_code == 0
    assert output[-1]["kind"] == "scan_completed"
    assert output[-1]["payload"]["simulated_inference"] is True
    run_dir = tmp_path / "strix_runs" / "real-strix-dry-run"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "completed"
    assert len(json.loads((run_dir / "vulnerabilities.json").read_text())) == 1
    assert {
        "findings.sarif",
        "penetration_test_report.md",
        "run.json",
        "vulnerabilities.csv",
        "vulnerabilities.json",
    } <= {path.name for path in run_dir.iterdir()}
    assert list((run_dir / "vulnerabilities").glob("vuln-*.md"))
    after = {
        container["Id"]
        for container in docker_client.api.containers(all=True, filters=strix_filter)
    }
    docker_client.close()
    assert after == before
    bridge_events = [event for event in output if event["kind"] == "bridge_event"]
    cleanup = [event for event in bridge_events if event.get("event_kind") == "sandbox_closed"]
    assert cleanup and cleanup[-1]["payload"]["deletion_verified"] is True
