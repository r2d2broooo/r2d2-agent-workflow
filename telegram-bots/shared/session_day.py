"""London-day gate for dashboard and analysis sessions.

A new calendar day in Europe/London starts a fresh native session.
The same day continues, so the current conversation can finish.
Older turns stay in the chat log and are not sent unless a task asks.
A session whose context grows past agents.CONTEXT_RESET_TOKENS is reset early (reset()).
Paper-trading calls do not use this marker.
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
MARKER = ".r2d2-session-day"


def today() -> str:
    return datetime.now(LONDON).date().isoformat()


def marker_path(cwd: str) -> Path:
    return Path(cwd) / MARKER


def same_day(cwd: str) -> bool:
    try:
        return marker_path(cwd).read_text().strip() == today()
    except OSError:
        return False


def mark(cwd: str) -> None:
    path = marker_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(today() + "\n")


def reset(cwd: str) -> None:
    """Start a fresh native session on the next message (a session grew too large)."""
    marker_path(cwd).unlink(missing_ok=True)
