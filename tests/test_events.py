from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest

from strix_claude_bridge.events import JsonlEventWriter, to_secret_safe


@dataclass
class Message:
    session_id: str
    usage: dict[str, object]


class DangerousRepr:
    def __repr__(self) -> str:
        return "Bearer should-never-be-read"


def test_recursive_secret_redaction_and_safe_unknown_fallback() -> None:
    payload = {
        "api_key": "sk-ant-secret-value",
        "nested": {
            "Authorization": "Bearer abc.def.ghi",
            "safe": "prefix sk-ant-12345678remaining suffix",
        },
        "unknown": DangerousRepr(),
    }

    encoded = json.dumps(to_secret_safe(payload))

    assert "secret-value" not in encoded
    assert "abc.def.ghi" not in encoded
    assert "remaining" not in encoded
    assert "should-never-be-read" not in encoded
    assert encoded.count("[REDACTED]") >= 3
    assert "DangerousRepr" in encoded


@pytest.mark.asyncio
async def test_jsonl_writer_emits_parseable_dataclass_envelope() -> None:
    output = io.StringIO()
    writer = JsonlEventWriter(output)

    await writer.emit(
        "sdk_message",
        Message(session_id="session-1", usage={"input_tokens": 3}),
        message_type="Message",
    )

    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["schema_version"] == 1
    assert event["kind"] == "sdk_message"
    assert event["message_type"] == "Message"
    assert event["payload"]["session_id"] == "session-1"
    assert event["payload"]["usage"]["input_tokens"] == 3
