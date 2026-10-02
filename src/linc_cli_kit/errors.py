"""Error envelope + exit-code classification (spec §5.2)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

CATEGORIES = (
    "validation",
    "not_found",
    "permission",
    "unavailable",
    "upstream_timeout",
    "internal",
)
_RETRYABLE = {"unavailable", "upstream_timeout"}


class ToolError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "TOOL_ERROR",
        hint: str = "",
        category: str = "internal",
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
        self.category = category if category in CATEGORIES else "internal"
        self.retryable = (self.category in _RETRYABLE) if retryable is None else retryable
        self.details = details or {}
        self.exit_code = exit_code

    def envelope(self) -> dict[str, Any]:
        return {"error": {
            "code": self.code, "message": self.message, "hint": self.hint,
            "category": self.category, "retryable": self.retryable, "details": self.details,
        }}


class UsageError(ToolError):
    def __init__(
        self,
        message: str,
        *,
        hint: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code="USAGE_ERROR",
            hint=hint,
            category="validation",
            retryable=False,
            details=details,
            exit_code=2,
        )


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    payload: Any
    envelope: dict[str, Any] | None = field(default=None)


def safe_str(value: Any) -> str:
    """str() that cannot raise: the error path must not fail on a hostile __str__."""
    try:
        return str(value)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return f"<unprintable {type(value).__name__}>"


def _upper_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


def classify(result: Any) -> Outcome:
    """Map a tool's return value to an Outcome using the fleet's dict conventions."""
    if result is None:
        return Outcome(0, {}, None)
    # Truthiness, not key presence: tools that always include an "error" key and set it to
    # None/"" on success must classify as success (fleet convention).
    if isinstance(result, dict) and result.get("error"):
        validation = bool(result.get("validation_error"))
        err = ToolError(
            safe_str(result["error"]),
            code="VALIDATION_ERROR" if validation else "TOOL_ERROR",
            category="validation" if validation else "internal",
            details={k: v for k, v in result.items() if k not in ("error", "validation_error")},
            exit_code=2 if validation else 1,
        )
        return Outcome(err.exit_code, None, err.envelope())
    return Outcome(0, result, None)


def parse_error_envelope(stderr: str) -> dict[str, Any] | None:
    """Return the error envelope from a command's stderr, or None if it wrote none.

    The contract: on failure the envelope is the LAST line of stderr. Lines before it may be
    JSON heartbeats (`{"event": "heartbeat", ...}`) or stderr logging, so never
    `json.loads` the whole stream — take the last line that parses as an object with an
    "error" key, which is what this does.
    """
    for line in reversed(stderr.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
            return doc
    return None


def classify_exception(exc: BaseException) -> Outcome:
    if isinstance(exc, ToolError):
        return Outcome(exc.exit_code, None, exc.envelope())
    err = ToolError(
        safe_str(exc) or exc.__class__.__name__, code=_upper_snake(exc.__class__.__name__)
    )
    return Outcome(1, None, err.envelope())
