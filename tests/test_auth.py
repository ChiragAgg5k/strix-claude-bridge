import pytest

from strix_claude_bridge.auth import AuthenticationModeError, require_subscription_environment


def test_subscription_environment_accepts_absent_overrides() -> None:
    require_subscription_environment({"PATH": "/bin"})


@pytest.mark.parametrize(
    "variable",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ],
)
def test_subscription_environment_refuses_overrides_without_leaking_value(variable: str) -> None:
    secret = "sk-ant-do-not-print-this"

    with pytest.raises(AuthenticationModeError) as caught:
        require_subscription_environment({variable: secret})

    message = str(caught.value)
    assert variable in message
    assert secret not in message
    assert "values hidden" in message
