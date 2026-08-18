"""Secret-safe JSON Lines event serialization."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

_SECRET_KEY = re.compile(
    r"(?:^|[_-])(api[_-]?key|authorization|cookie|credential|oauth|password|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(\b(?:sk-ant|sk)-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL),
)
_REDACTED = "[REDACTED]"


def _safe_string(value: str, *, max_string: int) -> str:
    result = value
    for pattern in _SECRET_VALUE_PATTERNS:
        result = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + _REDACTED, result
        )
    if len(result) > max_string:
        return result[:max_string] + f"…[truncated {len(result) - max_string} chars]"
    return result


def to_secret_safe(
    value: Any,
    *,
    max_depth: int = 12,
    max_string: int = 16_384,
    _depth: int = 0,
) -> Any:
    """Convert SDK values to JSON-compatible data without exposing common secrets.

    Unknown objects are represented only by their class name. Their ``repr`` is deliberately
    never used because it can contain credentials.
    """
    if _depth >= max_depth:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_string(value, max_string=max_string)
    if isinstance(value, bytes):
        return f"[BYTES:{len(value)}]"
    if isinstance(value, (Path, Enum)):
        return to_secret_safe(
            value.value if isinstance(value, Enum) else str(value),
            max_depth=max_depth,
            max_string=max_string,
            _depth=_depth + 1,
        )
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_secret_safe(
            {field.name: getattr(value, field.name) for field in dataclasses.fields(value)},
            max_depth=max_depth,
            max_string=max_string,
            _depth=_depth + 1,
        )
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            result[key] = (
                _REDACTED
                if _SECRET_KEY.search(key)
                else to_secret_safe(
                    item,
                    max_depth=max_depth,
                    max_string=max_string,
                    _depth=_depth + 1,
                )
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            to_secret_safe(
                item,
                max_depth=max_depth,
                max_string=max_string,
                _depth=_depth + 1,
            )
            for item in value
        ]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return to_secret_safe(
                model_dump(mode="json"),
                max_depth=max_depth,
                max_string=max_string,
                _depth=_depth + 1,
            )
        except (TypeError, ValueError):
            pass
    return {"unserialized_type": type(value).__name__}


class JsonlEventWriter:
    """Serialize event envelopes atomically to a text stream."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._lock = asyncio.Lock()

    async def emit(self, kind: str, payload: Any = None, **fields: Any) -> None:
        envelope = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **fields,
            "payload": to_secret_safe(payload),
        }
        line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()
