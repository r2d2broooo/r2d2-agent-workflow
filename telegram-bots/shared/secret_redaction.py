"""Small, dependency-free secret redaction helpers.

Runtime credentials stay in ignored .env files. Any user/model text that is
persisted or logged still passes through this module so a pasted token cannot
be copied into chat history, reports, predictions, or diagnostics.
"""

from __future__ import annotations

import os
import re

TELEGRAM_TOKEN_RE = re.compile(r"(?<![0-9])\d{8,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|REFRESH[_-]?TOKEN|PRIVATE[_-]?KEY)[A-Z0-9_]*)\b\s*[:=]\s*)([\"']?)([^\s\"',;&]+)\2"
)
SECRET_NAME_RE = re.compile(
    r"(?i)(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|REFRESH[_-]?TOKEN|PRIVATE[_-]?KEY)"
)
_PLACEHOLDERS = {"", "change-me", "changeme", "your_token", "your-token", "your_secret", "...", "none", "null"}


def _configured_secret_values() -> list[tuple[str, str]]:
    values = []
    for name, value in os.environ.items():
        value = (value or "").strip()
        if not value or len(value) < 6 or not SECRET_NAME_RE.search(name):
            continue
        if value.lower() in _PLACEHOLDERS:
            continue
        values.append((name, value))
    return sorted(values, key=lambda item: len(item[1]), reverse=True)


def redact_secrets(value) -> str:
    """Replace credential-shaped values without exposing the original text."""
    text = "" if value is None else str(value)
    for name, secret in _configured_secret_values():
        text = text.replace(secret, f"[REDACTED {name}]")
    text = TELEGRAM_TOKEN_RE.sub("[REDACTED TELEGRAM TOKEN]", text)

    def replace_assignment(match: re.Match) -> str:
        return f"{match.group(1)}[REDACTED SECRET]"

    return SECRET_ASSIGNMENT_RE.sub(replace_assignment, text)


def contains_secret(value) -> bool:
    original = "" if value is None else str(value)
    return redact_secrets(original) != original
