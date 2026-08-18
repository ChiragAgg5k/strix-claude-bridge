"""Command-line entry point for the isolated compatibility spike."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path

from strix_claude_bridge.auth import AuthenticationModeError, require_subscription_environment
from strix_claude_bridge.backend import BackendStateError, SessionCompatibilityError
from strix_claude_bridge.events import JsonlEventWriter
from strix_claude_bridge.single_agent import SingleAgentScanError
from strix_claude_bridge.strix_integration import IntegrationCompatibilityError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strix-claude-bridge",
        description="Experimental local Strix execution through the official Claude Agent SDK.",
        epilog=(
            "AUTHORIZED USE ONLY. Live execution requires an existing supported local Claude "
            "login and never implements or inspects login credentials."
        ),
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    scan = subparsers.add_parser(
        "scan",
        help="run one real Strix root agent through Claude Agent SDK",
    )
    scan.add_argument(
        "--experimental",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    scan.add_argument(
        "--authorized-use",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    scan.add_argument("--target", "-t", action="append", required=True)
    scan.add_argument("--instruction", default="")
    scan.add_argument("--scan-mode", choices=("quick", "standard", "deep"), default="deep")
    scan.add_argument("--run-name")
    scan.add_argument("--image", help="override Strix's configured Docker sandbox image")
    scan.add_argument("--model", help="optional Agent SDK model selector")
    scan.add_argument("--max-turns", type=int, default=100)
    scan.add_argument("--turn-timeout", type=float, metavar="SECONDS")
    scan.add_argument("--max-runtime", type=float, default=3600, metavar="SECONDS")
    scan.add_argument("--max-concurrent-agents", type=int, default=2)
    scan.add_argument("--max-agents", type=int, default=8)
    scan.add_argument("--max-tool-calls-per-agent", type=int, default=500)
    scan.add_argument(
        "--resume-token",
        help=("disabled safety guard: any value is rejected before target or Docker side effects"),
    )
    scan.add_argument("--config", help="Strix JSON configuration override")
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "no inference/credentials: execute the bundled fixture script through real Strix tools"
        ),
    )
    scan.add_argument(
        "--include-sensitive-content",
        action="store_true",
        help="include best-effort-redacted transcript/tool payloads in the bridge JSONL",
    )

    view = subparsers.add_parser("view", help="open the upstream Strix local viewer for a run")
    view.add_argument("run", nargs="?", default=None)
    view.add_argument("--port", type=int, default=0)
    view.add_argument("--host", default="127.0.0.1")
    view.add_argument("--no-open", action="store_true")

    export_pdf = subparsers.add_parser(
        "export-pdf", help="export a run's report as PDF via upstream Strix renderer"
    )
    export_pdf.add_argument("run", nargs="?", default=None)
    export_pdf.add_argument("--output")
    export_pdf.add_argument("--encrypt", action="store_true")

    export_html = subparsers.add_parser(
        "export-html", help="export a run's report as a standalone HTML artifact"
    )
    export_html.add_argument("run", nargs="?", default=None)
    export_html.add_argument("--output")
    return parser


def _prepare_scan_inputs(
    args: argparse.Namespace,
) -> tuple[dict[str, object], list[dict[str, object]], str]:
    """Resolve target strings with Strix's canonical target helpers."""
    if args.config:
        from strix.config import apply_config_override
        from strix.interface.cli_args import validate_config_file

        apply_config_override(validate_config_file(args.config))
    from strix.interface.scan_setup import build_targets_info
    from strix.interface.utils import (
        clone_repository,
        collect_local_sources,
        generate_run_name,
        stage_api_specs,
    )

    target_args = argparse.Namespace(target=args.target, target_list=[])
    build_targets_info(target_args)
    targets = target_args.targets_info
    run_name = args.run_name or generate_run_name(targets)
    for target in targets:
        if target["type"] == "repository":
            details = target["details"]
            details["cloned_repo_path"] = clone_repository(
                details["target_repo"], run_name, details.get("workspace_subdir")
            )
    local_sources = collect_local_sources(targets)
    local_sources.extend(stage_api_specs(targets, run_name))
    scan_config: dict[str, object] = {
        "scan_id": run_name,
        "run_name": run_name,
        "targets": targets,
        "user_instructions": args.instruction,
        "scan_mode": args.scan_mode,
        "non_interactive": True,
        "local_sources": local_sources,
        "workspace_files": [],
        "scope_mode": "full",
        "diff_scope": {"active": False},
    }
    return scan_config, local_sources, run_name


async def _scan(args: argparse.Namespace, writer: JsonlEventWriter) -> int:
    if not 1 <= args.max_turns <= 1000:
        await writer.emit("configuration_error", {"message": "--max-turns must be in [1, 1000]"})
        return 2
    if args.turn_timeout is not None and args.turn_timeout <= 0:
        await writer.emit("configuration_error", {"message": "--turn-timeout must be positive"})
        return 2
    if args.max_runtime <= 0:
        await writer.emit("configuration_error", {"message": "--max-runtime must be positive"})
        return 2
    if not 1 <= args.max_concurrent_agents <= args.max_agents:
        await writer.emit(
            "configuration_error",
            {"message": "--max-concurrent-agents must be in [1, --max-agents]"},
        )
        return 2
    if args.max_tool_calls_per_agent is not None and args.max_tool_calls_per_agent < 1:
        await writer.emit(
            "configuration_error", {"message": "--max-tool-calls-per-agent must be positive"}
        )
        return 2
    if args.resume_token is not None:
        await writer.emit(
            "configuration_error",
            {
                "message": (
                    "--resume-token is disabled until durable multi-agent graph restoration "
                    "and provider tool-use reconciliation are implemented"
                )
            },
        )
        return 2

    from strix.config import load_settings

    from strix_claude_bridge.claude_backend import ClaudeAgentSDKBackend
    from strix_claude_bridge.multi_agent import MultiAgentScanConfig, run_multi_agent_scan
    from strix_claude_bridge.strix_integration import verify_runtime_compatibility

    # Validate cheap local compatibility/auth invariants before target cloning,
    # API-spec staging, run artifact creation, or Docker side effects.
    verify_runtime_compatibility()
    if not args.dry_run:
        require_subscription_environment()

    scan_config, local_sources, run_name = _prepare_scan_inputs(args)
    backend = None
    if args.dry_run:
        targets = scan_config["targets"]
        if len(targets) != 1 or targets[0].get("type") != "local_code":
            raise ValueError("--dry-run requires exactly one local directory target")
        target_path = Path(targets[0]["details"]["target_path"])
        marker_file = target_path / "app.py"
        if "STRIX_DRY_RUN_PATH_TRAVERSAL" not in marker_file.read_text(encoding="utf-8"):
            raise ValueError("--dry-run target must be the intentionally vulnerable bridge fixture")
        from strix_claude_bridge.multi_agent_dry_run import (
            build_multi_agent_dry_run_client_factory,
        )

        workspace_subdir = str(targets[0]["details"]["workspace_subdir"])
        backend = ClaudeAgentSDKBackend(
            client_factory=build_multi_agent_dry_run_client_factory(
                workspace_subdir=workspace_subdir
            ),
            enforce_subscription_environment=False,
        )

    async def emit_bridge_event(event: object) -> None:
        payload = (
            getattr(event, "payload", {})
            if (args.include_sensitive_content or not getattr(event, "sensitive", False))
            else {"content_omitted": True}
        )
        await writer.emit(
            "bridge_event",
            payload,
            event_kind=getattr(event, "kind", "unknown"),
            agent_id=getattr(event, "agent_id", "root"),
            sequence=getattr(event, "sequence", 0),
            sensitive=getattr(event, "sensitive", False),
        )

    await writer.emit(
        "scan_started",
        {
            "run_name": run_name,
            "backend": "claude-agent-sdk",
            "experimental": True,
            "simulated_inference": bool(args.dry_run),
        },
    )
    outcome = await run_multi_agent_scan(
        MultiAgentScanConfig(
            scan_config=scan_config,
            run_name=run_name,
            image=args.image or load_settings().runtime.image,
            local_sources=local_sources,
            model=args.model,
            max_turns=args.max_turns,
            turn_timeout_s=args.turn_timeout,
            max_runtime_s=args.max_runtime,
            max_concurrent_agents=args.max_concurrent_agents,
            max_agents=args.max_agents,
            max_tool_calls_per_agent=args.max_tool_calls_per_agent,
            resume_token=args.resume_token,
        ),
        backend=backend,
        event_sink=emit_bridge_event,
        simulated_inference=bool(args.dry_run),
    )
    await writer.emit(
        "scan_completed",
        {
            "run_name": outcome.run_name,
            "run_dir": str(outcome.run_dir),
            "completed": outcome.completed,
            "vulnerability_count": outcome.vulnerability_count,
            "terminal_reason": outcome.terminal_reason,
            "simulated_inference": outcome.simulated_inference,
        },
    )
    return 0


def _view(args: argparse.Namespace) -> int:
    from strix.interface.viewer.cli import run_view

    argv: list[str] = []
    if args.run:
        argv.append(args.run)
    if args.port:
        argv.extend(["--port", str(args.port)])
    if args.host != "127.0.0.1":
        argv.extend(["--host", args.host])
    if args.no_open:
        argv.append("--no-open")
    try:
        run_view(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1
    return 0


async def _export_pdf(args: argparse.Namespace, writer: JsonlEventWriter) -> int:
    from strix_claude_bridge.export import export_pdf

    path, password = export_pdf(args.run, output=args.output, encrypt=args.encrypt)
    await writer.emit(
        "export_completed",
        {
            "format": "pdf",
            "path": str(path),
            "encrypted": bool(password),
        },
    )
    if password is not None:
        print(f"PDF password: {password}")
    return 0


async def _export_html(args: argparse.Namespace, writer: JsonlEventWriter) -> int:
    from strix_claude_bridge.export import export_html

    path = export_html(args.run, output=args.output)
    await writer.emit("export_completed", {"format": "html", "path": str(path)})
    return 0


async def _run(args: argparse.Namespace) -> int:
    writer = JsonlEventWriter()
    try:
        if args.operation == "scan":
            return await _scan(args, writer)
        if args.operation == "view":
            return _view(args)
        if args.operation == "export-pdf":
            return await _export_pdf(args, writer)
        return await _export_html(args, writer)
    except AuthenticationModeError as exc:
        await writer.emit("authentication_error", {"message": str(exc)})
        return 2
    except (
        IntegrationCompatibilityError,
        SessionCompatibilityError,
        BackendStateError,
        SingleAgentScanError,
        ValueError,
    ) as exc:
        await writer.emit("compatibility_error", {"message": str(exc)})
        return 2
    except Exception as exc:
        # Exception text from the SDK or Docker daemon can contain arbitrary remote content.
        await writer.emit("error", {"error_type": type(exc).__name__})
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Avoid a traceback when JSONL is intentionally piped to a closed consumer.
        with contextlib.suppress(OSError):
            sys.stdout.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
