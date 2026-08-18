from strix_claude_bridge import cli
from strix_claude_bridge.auth import AuthenticationModeError
from strix_claude_bridge.backend import BackendStateError, SessionCompatibilityError
from strix_claude_bridge.cli import build_parser, main


def test_probe_commands_are_not_exposed() -> None:
    operations = next(
        action for action in build_parser()._actions if action.dest == "operation"
    ).choices

    assert "sandbox-probe" not in operations
    assert "live-probe" not in operations


def test_scan_dry_run_still_validates_limits(capsys) -> None:
    exit_code = main(["scan", "--target", ".", "--dry-run", "--max-turns", "0"])

    assert exit_code == 2
    assert "--max-turns must be in [1, 1000]" in capsys.readouterr().out


def test_multi_agent_limits_and_disabled_resume_are_validated_before_side_effects(
    monkeypatch, capsys
) -> None:
    exit_code = main(
        [
            "scan",
            "--dry-run",
            "--target",
            ".",
            "--max-concurrent-agents",
            "3",
            "--max-agents",
            "2",
        ]
    )
    assert exit_code == 2
    assert "max-concurrent-agents" in capsys.readouterr().out

    exit_code = main(
        ["scan", "--dry-run", "--target", ".", "--resume-token", "x"]
    )
    assert exit_code == 2
    assert "resume-token is disabled" in capsys.readouterr().out

    prepared = False

    def prepare(_args: object) -> object:
        nonlocal prepared
        prepared = True
        raise AssertionError("empty resume token must be rejected before target preparation")

    monkeypatch.setattr(cli, "_prepare_scan_inputs", prepare)
    exit_code = main(["scan", "--dry-run", "--target", ".", "--resume-token", ""])
    assert exit_code == 2
    assert prepared is False
    assert "resume-token is disabled" in capsys.readouterr().out


def test_scan_has_finite_default_agent_tool_budget() -> None:
    args = build_parser().parse_args(["scan", "--target", "."])
    assert args.max_tool_calls_per_agent == 500


def test_view_and_export_commands_parse() -> None:
    args = build_parser().parse_args(["view", "sample-run", "--port", "4310", "--no-open"])
    assert args.operation == "view"
    assert args.run == "sample-run"
    assert args.port == 4310
    assert args.no_open is True

    args = build_parser().parse_args(["export-pdf", "sample-run", "--output", "out.pdf"])
    assert args.operation == "export-pdf"
    assert args.run == "sample-run"
    assert args.output == "out.pdf"
    assert args.encrypt is False

    args = build_parser().parse_args(["export-html", "sample-run", "--output", "out.html"])
    assert args.operation == "export-html"
    assert args.run == "sample-run"
    assert args.output == "out.html"


def test_live_scan_validates_auth_before_target_side_effects(monkeypatch, capsys) -> None:
    prepared = False

    def prepare(_args: object) -> object:
        nonlocal prepared
        prepared = True
        raise AssertionError("target preparation must not run")

    def reject_auth() -> None:
        raise AuthenticationModeError("no supported subscription environment")

    monkeypatch.setattr(cli, "_prepare_scan_inputs", prepare)
    monkeypatch.setattr(cli, "require_subscription_environment", reject_auth)

    exit_code = main(["scan", "--target", "https://example.test"])

    assert exit_code == 2
    assert prepared is False
    assert '"kind":"authentication_error"' in capsys.readouterr().out


def test_known_bridge_state_errors_are_actionable(monkeypatch, capsys) -> None:
    errors = [
        SessionCompatibilityError("checkpoint model does not match"),
        BackendStateError("cumulative provider turn limit is exhausted"),
    ]

    async def fail_scan(_args: object, _writer: object) -> int:
        raise errors.pop(0)

    monkeypatch.setattr(cli, "_scan", fail_scan)
    command = ["scan", "--dry-run", "--target", "."]
    assert main(command) == 2
    assert "checkpoint model does not match" in capsys.readouterr().out
    assert main(command) == 2
    assert "cumulative provider turn limit is exhausted" in capsys.readouterr().out


def test_view_dispatches_through_cli_wrapper(monkeypatch) -> None:
    called = {}

    def fake_view(args: object) -> int:
        called["operation"] = getattr(args, "operation", None)
        called["run"] = getattr(args, "run", None)
        return 0

    monkeypatch.setattr(cli, "_view", fake_view)

    assert main(["view", "sample-run", "--no-open"]) == 0
    assert called == {"operation": "view", "run": "sample-run"}
