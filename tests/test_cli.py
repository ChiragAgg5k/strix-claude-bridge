from strix_claude_bridge import cli, probe
from strix_claude_bridge.auth import AuthenticationModeError
from strix_claude_bridge.backend import BackendStateError, SessionCompatibilityError
from strix_claude_bridge.cli import build_parser, main


def test_sandbox_command_does_not_overwrite_subcommand() -> None:
    args = build_parser().parse_args(["sandbox-probe", "--command", "printf ok"])

    assert args.operation == "sandbox-probe"
    assert args.sandbox_command == "printf ok"


def test_live_probe_requires_explicit_authorized_use(capsys) -> None:
    exit_code = main(["live-probe", "--prompt", "safe"])

    assert exit_code == 2
    assert '"kind":"configuration_error"' in capsys.readouterr().out


def test_scan_is_gated_as_experimental(capsys) -> None:
    exit_code = main(["scan", "--target", ".", "--dry-run"])

    assert exit_code == 2
    assert "requires --experimental" in capsys.readouterr().out


def test_multi_agent_limits_and_disabled_resume_are_validated_before_side_effects(
    monkeypatch, capsys
) -> None:
    exit_code = main(
        [
            "scan",
            "--experimental",
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
        ["scan", "--experimental", "--dry-run", "--target", ".", "--resume-token", "x"]
    )
    assert exit_code == 2
    assert "resume-token is disabled" in capsys.readouterr().out

    prepared = False

    def prepare(_args: object) -> object:
        nonlocal prepared
        prepared = True
        raise AssertionError("empty resume token must be rejected before target preparation")

    monkeypatch.setattr(cli, "_prepare_scan_inputs", prepare)
    exit_code = main(["scan", "--experimental", "--dry-run", "--target", ".", "--resume-token", ""])
    assert exit_code == 2
    assert prepared is False
    assert "resume-token is disabled" in capsys.readouterr().out


def test_scan_has_finite_default_agent_tool_budget() -> None:
    args = build_parser().parse_args(["scan", "--target", "."])
    assert args.max_tool_calls_per_agent == 500


def test_live_scan_requires_authorization(capsys) -> None:
    exit_code = main(["scan", "--experimental", "--target", "."])

    assert exit_code == 2
    assert "requires --authorized-use" in capsys.readouterr().out


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

    exit_code = main(
        ["scan", "--experimental", "--authorized-use", "--target", "https://example.test"]
    )

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
    command = ["scan", "--experimental", "--dry-run", "--target", "."]
    assert main(command) == 2
    assert "checkpoint model does not match" in capsys.readouterr().out
    assert main(command) == 2
    assert "cumulative provider turn limit is exhausted" in capsys.readouterr().out


def test_terminal_failure_returns_nonzero_without_error_text(monkeypatch, capsys) -> None:
    async def fail_probe(**_kwargs: object) -> None:
        raise probe.ProbeTerminalError("sensitive provider text")

    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(probe, "run_live_probe", fail_probe)

    exit_code = main(["live-probe", "--authorized-use", "--prompt", "safe"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert '"error_type":"ProbeTerminalError"' in output
    assert "sensitive provider text" not in output
