"""Standalone HTML report rendering for completed bridge runs."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from strix.interface.viewer.transcript import (
    read_report_markdown,
    read_run_summary,
    read_vulnerabilities,
    severity_counts,
)


def render_html_report(run_dir: Path) -> str:
    record = read_run_summary(run_dir)
    vulnerabilities = [item for item in read_vulnerabilities(run_dir) if isinstance(item, dict)]
    counts = severity_counts(vulnerabilities)
    report_markdown = read_report_markdown(run_dir)
    run_name = str(record.get("run_name") or run_dir.name)
    status = str(record.get("status") or "unknown")
    target = _primary_target(record)

    finding_rows = "\n".join(
        _render_finding(index, finding)
        for index, finding in enumerate(vulnerabilities, start=1)
    )
    if not finding_rows:
        finding_rows = '<p class="empty">No findings were recorded for this run.</p>'

    summary_pairs = {
        "Run": run_name,
        "Status": status,
        "Target": target or "—",
        "Started": str(record.get("start_time") or "—"),
        "Finished": str(record.get("end_time") or "—"),
        "Findings": str(len(vulnerabilities)),
    }
    summary_html = "\n".join(
        f"<tr><th>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>"
        for key, value in summary_pairs.items()
    )
    counts_html = "".join(
        f'<li><strong>{html.escape(level.title())}</strong><span>{counts[level]}</span></li>'
        for level in ("critical", "high", "medium", "low")
    )
    record_json = html.escape(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    )

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{html.escape(run_name)} · Strix Claude Bridge Report</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      margin: 0;
      background: #0b1020;
      color: #e5e7eb;
    }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 32px 20px 64px; }}
    h1, h2, h3 {{ margin: 0 0 12px; }}
    .muted {{ color: #94a3b8; }}
    .grid {{ display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 20px; margin: 24px 0; }}
    .card {{
      background: #111827;
      border: 1px solid #1f2937;
      border-radius: 14px;
      padding: 20px;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      text-align: left;
      padding: 10px 0;
      border-bottom: 1px solid #1f2937;
      vertical-align: top;
    }}
    th {{ width: 180px; color: #94a3b8; font-weight: 600; }}
    ul.counts {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }}
    ul.counts li {{
      display: flex;
      justify-content: space-between;
      padding: 12px 14px;
      background: #0f172a;
      border-radius: 10px;
    }}
    .finding {{
      margin: 16px 0 0;
      padding: 16px;
      border: 1px solid #1f2937;
      border-radius: 12px;
      background: #0f172a;
    }}
    .finding h3 {{ margin-bottom: 8px; }}
    .label {{
      display: inline-block;
      padding: 4px 8px;
      border-radius: 999px;
      background: #1d4ed8;
      font-size: 12px;
      margin-bottom: 8px;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #020617;
      border: 1px solid #1f2937;
      border-radius: 12px;
      padding: 16px;
      overflow-x: auto;
    }}
    .empty {{ color: #94a3b8; }}
    @media (max-width: 860px) {{
      .grid {{ grid-template-columns: 1fr; }}
      th {{ width: 120px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{html.escape(run_name)}</h1>
      <p class=\"muted\">Standalone report exported from Strix Claude Bridge run artifacts.</p>
    </header>
    <section class=\"grid\">
      <div class=\"card\">
        <h2>Run summary</h2>
        <table>{summary_html}</table>
      </div>
      <div class=\"card\">
        <h2>Severity counts</h2>
        <ul class=\"counts\">{counts_html}</ul>
      </div>
    </section>
    <section class=\"card\">
      <h2>Findings</h2>
      {finding_rows}
    </section>
    <section class=\"card\" style=\"margin-top:20px\">
      <h2>Markdown report</h2>
      <pre>{html.escape(report_markdown or 'No markdown report was recorded for this run.')}</pre>
    </section>
    <section class=\"card\" style=\"margin-top:20px\">
      <h2>Raw run record</h2>
      <pre>{record_json}</pre>
    </section>
  </main>
</body>
</html>
"""


def _render_finding(index: int, finding: dict[str, Any]) -> str:
    title = str(
        finding.get("title")
        or finding.get("name")
        or finding.get("summary")
        or f"Finding {index}"
    )
    severity = str(finding.get("severity") or "unknown")
    location = str(
        finding.get("location")
        or finding.get("target")
        or finding.get("affected_asset")
        or finding.get("endpoint")
        or ""
    )
    details = str(
        finding.get("description")
        or finding.get("technical_details")
        or finding.get("details")
        or finding.get("evidence")
        or ""
    )
    extras = []
    for key in ("impact", "recommendation", "remediation", "cwe"):
        value = finding.get(key)
        if value:
            extras.append(
                f"<p><strong>{html.escape(key.title())}:</strong> "
                f"{html.escape(str(value))}</p>"
            )
    location_html = f"<p><strong>Location:</strong> {html.escape(location)}</p>" if location else ""
    detail_html = f"<p>{html.escape(details)}</p>" if details else ""
    return (
        f'<article class="finding">'
        f'<div class="label">{html.escape(severity.title())}</div>'
        f"<h3>{index}. {html.escape(title)}</h3>"
        f"{location_html}{detail_html}{''.join(extras)}"
        f"</article>"
    )


def _primary_target(record: dict[str, Any]) -> str | None:
    targets = record.get("targets_info")
    if not isinstance(targets, list):
        return None
    for entry in targets:
        if isinstance(entry, dict):
            original = entry.get("original")
            if isinstance(original, str) and original:
                return original
    return None
