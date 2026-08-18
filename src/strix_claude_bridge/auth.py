"""Authentication boundary checks for an explicitly approved local subscription probe."""

from __future__ import annotations

import os


class AuthenticationModeError(RuntimeError):
    """Raised when ambient environment configuration defeats the requested auth mode."""


# Documented Claude Code/Agent SDK environment selectors that can override a local
# subscription login or route requests through API/cloud-provider authentication.
_AUTH_OVERRIDE_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def require_subscription_environment(environ: dict[str, str] | None = None) -> None:
    """Reject API-key precedence without inspecting or printing key values.

    This check does not assert that a subscription login exists, identify an organization, or
    authenticate the user. Authentication remains entirely owned by Anthropic's local tooling.
    """
    environment = os.environ if environ is None else environ
    conflicting = [name for name in _AUTH_OVERRIDE_VARIABLES if environment.get(name)]
    if conflicting:
        names = ", ".join(conflicting)
        raise AuthenticationModeError(
            f"Claude subscription mode refused: unset {names}; "
            "an API/cloud authentication override "
            "would defeat the requested local subscription authentication mode (values hidden)"
        )
