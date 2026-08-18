"""Bridge export helpers that wrap upstream Strix viewer/report functionality."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from strix.core.paths import latest_run_dir, run_dir_for, run_record_path, runs_base_dir
from strix.interface.viewer.report_pdf import build_encrypted_report, generate_report_pdf

from strix_claude_bridge.html_report import render_html_report

_DEFAULT_PDF_PREFIX: Final = "strix-report"
_DEFAULT_HTML_PREFIX: Final = "strix-report"


def resolve_run_dir(run: str | None) -> Path:
    if run:
        run_dir = run_dir_for(run)
        if run_record_path(run_dir).is_file():
            return run_dir
        raise ValueError(f"no run named '{run}' under ./{runs_base_dir().name}")
    latest = latest_run_dir()
    if latest is not None:
        return latest
    raise ValueError(f"no runs found under ./{runs_base_dir().name}")


def export_pdf(run: str | None, *, output: str | None, encrypt: bool) -> tuple[Path, str | None]:
    run_dir = resolve_run_dir(run)
    run_name = run_dir.name
    if encrypt:
        pdf_bytes, password, default_name = build_encrypted_report(run_dir)
        output_path = Path(output) if output else run_dir / default_name
    else:
        pdf_bytes = generate_report_pdf(run_dir)
        password = None
        output_path = Path(output) if output else run_dir / f"{_DEFAULT_PDF_PREFIX}-{run_name}.pdf"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(pdf_bytes)
    return output_path, password


def export_html(run: str | None, *, output: str | None) -> Path:
    run_dir = resolve_run_dir(run)
    run_name = run_dir.name
    output_path = Path(output) if output else run_dir / f"{_DEFAULT_HTML_PREFIX}-{run_name}.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html_report(run_dir), encoding="utf-8")
    return output_path
