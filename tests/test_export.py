from __future__ import annotations

import json
from pathlib import Path

from strix_claude_bridge.export import export_html, export_pdf


def _write_run(run_dir: Path) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_name": run_dir.name,
                "status": "completed",
                "start_time": "2025-01-01T00:00:00+00:00",
                "end_time": "2025-01-01T00:10:00+00:00",
                "targets_info": [{"original": "https://example.test"}],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "vulnerabilities.json").write_text(
        json.dumps(
            [
                {
                    "title": "Path traversal",
                    "severity": "high",
                    "description": "Arbitrary file read via .. segments.",
                    "recommendation": "Normalize and confine paths.",
                }
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "penetration_test_report.md").write_text(
        "# Executive summary\n\nOne high-severity issue was confirmed.",
        encoding="utf-8",
    )


def test_export_html_writes_standalone_artifact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "strix_runs" / "sample-run"
    _write_run(run_dir)

    output = export_html("sample-run", output=None)

    text = output.read_text(encoding="utf-8")
    assert output.name == "strix-report-sample-run.html"
    assert "sample-run" in text
    assert "Path traversal" in text
    assert "Executive summary" in text


def test_export_pdf_writes_pdf_artifact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "strix_runs" / "sample-run"
    _write_run(run_dir)

    output, password = export_pdf("sample-run", output=str(tmp_path / "report.pdf"), encrypt=False)

    assert password is None
    assert output.read_bytes().startswith(b"%PDF")
