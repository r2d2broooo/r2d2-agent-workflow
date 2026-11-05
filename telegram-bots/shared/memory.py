import logging
import os
import sqlite3
import time
from typing import Awaitable, Callable

from . import activity, chat_log, session_memory

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "chat_log.db")

DAILY_REPORT_PROMPT = """You write a durable end-of-day learning review for an ongoing work log.

Conversation and workspace evidence from {date}:
---
{transcript}
---

Write a compressed review: a short bullet list (max ~12 bullets) preserving \
valuable facts/data, decisions, lessons learned, strategy or workflow changes, \
mindset changes, self-assessments, mistakes and their corrections, and explicit \
open questions. Include concrete numbers or evidence when present. Drop only \
small talk and noise. Do not invent conclusions. Output only the bullet list, \
nothing else."""

SESSION_GAP_SECONDS = 1800  # messages more than 30 min apart start a new session


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory (
            chat_id INTEGER NOT NULL,
            bot_name TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            PRIMARY KEY (chat_id, bot_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_reports (
            chat_id INTEGER NOT NULL,
            bot_name TEXT NOT NULL,
            date TEXT NOT NULL,
            report TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (chat_id, bot_name, date)
        )
        """
    )
    try:
        conn.execute("ALTER TABLE daily_reports ADD COLUMN folded INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            text TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(category, text, source)
        )
        """
    )
    return conn


def add_learning_entry(category: str, text: str, source: str = "manual") -> bool:
    """Persist one durable lesson, decision, assessment, or evidence record."""
    category = (category or "lesson").strip().lower()[:40]
    text = " ".join((text or "").strip().split())[:4_000]
    source = (source or "manual").strip()[:160]
    if not text:
        return False
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO learning_ledger (category, text, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(category, text, source) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (category, text, source, now, now),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_learning_entries(limit: int = 200) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, category, text, source, created_at, updated_at
            FROM learning_ledger
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [
            {"id": r[0], "category": r[1], "text": r[2], "source": r[3],
             "created_at": r[4], "updated_at": r[5]}
            for r in rows
        ]
    finally:
        conn.close()


def get_long_term(chat_id: int, bot_name: str) -> str:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT summary FROM memory WHERE chat_id = ? AND bot_name = ?",
            (chat_id, bot_name),
        ).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def get_long_term_metadata(chat_id: int, bot_name: str) -> dict:
    """Return freshness/provenance metadata without exposing the full summary."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT updated_at FROM memory WHERE chat_id = ? AND bot_name = ?",
            (chat_id, bot_name),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return {"available": False, "source": bot_name, "generated_by": "memory summarizer"}
    age_seconds = max(0.0, time.time() - float(row[0]))
    if age_seconds < 3600:
        age = f"{max(1, int(age_seconds // 60))}m"
    elif age_seconds < 86400:
        age = f"{max(1, int(age_seconds // 3600))}h"
    else:
        age = f"{max(1, int(age_seconds // 86400))}d"
    max_age = int(os.environ.get("R2D2_MEMORY_MAX_AGE_DAYS", "30")) * 86400
    return {
        "available": True,
        "source": bot_name,
        "generated_by": "memory summarizer",
        "updated_at": float(row[0]),
        "age": age,
        "age_seconds": round(age_seconds),
        "stale": age_seconds > max_age,
        "freshness_threshold_days": max(1, max_age // 86400),
    }


def set_long_term(chat_id: int, bot_name: str, summary: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO memory (chat_id, bot_name, summary, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, bot_name) DO UPDATE SET summary = excluded.summary, updated_at = excluded.updated_at
            """,
            (chat_id, bot_name, summary, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


async def summarize_and_compact(
    chat_id: int,
    bot_name: str,
    messages: list[dict],
    agent_fn: Callable[[str], Awaitable[str]],
) -> str:
    """Compatibility entry point: archive messages verbatim without a model call."""
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
    session_memory.save_short(name=f"{bot_name} chat {chat_id}", model=bot_name,
                              kind="session", note=transcript)
    return transcript


def add_daily_report(chat_id: int, bot_name: str, date: str, report: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO daily_reports (chat_id, bot_name, date, report, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, bot_name, date) DO UPDATE SET report = excluded.report, created_at = excluded.created_at
            """,
            (chat_id, bot_name, date, report, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    # Retain the full report as an on-demand input; no second model pass.
    session_memory.save_short(name=f"{bot_name} chat {chat_id} daily {date}",
                              model=bot_name, kind="daily", note=report)
    # Keep the existing ledger and dashboard copies for compatibility.
    add_learning_entry("daily_review", report, f"daily-report:{chat_id}:{bot_name}:{date}")
    # Daily summaries are reports too: expose the same durable record in the
    # dashboard ledger, without creating a new card if a job is retried.
    try:
        try:
            report_ts = time.mktime(time.strptime(date, "%Y-%m-%d")) + (23 * 3600) + (55 * 60)
        except ValueError:
            report_ts = time.time()
        label = f"{chat_log.report_source_label(bot_name)} daily report · {date}"
        chat_log.add_report(
            chat_id, label, "", None, report, source=bot_name, report_type="daily",
            dedupe_key=f"daily-report:{chat_id}:{bot_name}:{date}",
            ts=report_ts,
        )
    except Exception:  # The memory record remains canonical if the display copy fails.
        log.exception("could not mirror daily report to dashboard (chat=%s bot=%s date=%s)",
                      chat_id, bot_name, date)


def get_daily_reports(chat_id: int, bot_name: str, limit: int = 30) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT date, report, created_at FROM daily_reports
            WHERE chat_id = ? AND bot_name = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (chat_id, bot_name, limit),
        ).fetchall()
        return [{"date": r[0], "report": r[1], "created_at": r[2]} for r in rows]
    finally:
        conn.close()


async def generate_daily_report(
    chat_id: int,
    bot_name: str,
    agent_fn: Callable[[str], Awaitable[str]],
    date: str | None = None,
) -> str | None:
    """Build a compressed end-of-day report from `date`'s messages and store it
    as long-term memory (one entry per day)."""
    date = date or time.strftime("%Y-%m-%d")
    msgs = [
        m for m in chat_log.get_recent_messages(chat_id, limit=1000)
        if m["bot_name"] in {bot_name, "dashboard"}
        and time.strftime("%Y-%m-%d", time.localtime(m["ts"])) == date
    ]
    if not msgs:
        return None
    transcript = "\n".join(f"{m['role']}: {m['text']}" for m in msgs)
    workspace = activity.day_summary(time.mktime(time.strptime(date, "%Y-%m-%d")))
    if workspace:
        transcript += "\n\nWORKSPACE ACTIVITY (system evidence, not conversation):\n" + workspace
    prompt = DAILY_REPORT_PROMPT.format(date=date, transcript=transcript)
    report = (await agent_fn(prompt)).strip()
    add_daily_report(chat_id, bot_name, date, report)
    return report


def get_unfolded_reports(chat_id: int, bot_name: str, older_than_days: int = 21) -> list[dict]:
    """Daily reports older than `older_than_days` that haven't been folded into
    long-term memory yet, oldest first."""
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - older_than_days * 86400))
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT date, report FROM daily_reports
            WHERE chat_id = ? AND bot_name = ? AND folded = 0 AND date < ?
            ORDER BY date ASC
            """,
            (chat_id, bot_name, cutoff),
        ).fetchall()
        return [{"date": r[0], "report": r[1]} for r in rows]
    finally:
        conn.close()


def mark_reports_folded(chat_id: int, bot_name: str, dates: list[str]) -> None:
    if not dates:
        return
    conn = _connect()
    try:
        conn.executemany(
            "UPDATE daily_reports SET folded = 1 WHERE chat_id = ? AND bot_name = ? AND date = ?",
            [(chat_id, bot_name, d) for d in dates],
        )
        conn.commit()
    finally:
        conn.close()


async def fold_reports(
    chat_id: int,
    bot_name: str,
    agent_fn: Callable[[str], Awaitable[str]],
    reports: list[dict],
) -> str:
    """Compatibility entry point: retain reports without calling the agent."""
    for report in reports:
        add_daily_report(chat_id, bot_name, report["date"], report["report"])
    return get_long_term(chat_id, bot_name)


async def compress_old_reports(
    chat_id: int,
    bot_name: str,
    agent_fn: Callable[[str], Awaitable[str]],
    older_than_days: int = 21,
) -> str | None:
    """Archive older reports verbatim, with no LLM call (legacy job interface)."""
    reports = get_unfolded_reports(chat_id, bot_name, older_than_days)
    if not reports:
        return None
    summary = await fold_reports(chat_id, bot_name, agent_fn, reports)
    mark_reports_folded(chat_id, bot_name, [r["date"] for r in reports])
    return summary


def get_recent_sessions(chat_id: int, bot_name: str, limit: int = 10) -> list[list[dict]]:
    """Group recent messages for chat/bot into sessions (gaps > SESSION_GAP_SECONDS
    start a new session) and return the last `limit` sessions, oldest first."""
    # Operator prompts typed in the dashboard are stored as bot_name="dashboard"; without them a
    # Group-room session showed only the bot's replies (same rule as generate_daily_report).
    msgs = [m for m in chat_log.get_recent_messages(chat_id, limit=1000)
            if m["bot_name"] in {bot_name, "dashboard"}]
    sessions: list[list[dict]] = []
    current: list[dict] = []
    last_ts: float | None = None
    for m in msgs:
        if last_ts is not None and m["ts"] - last_ts > SESSION_GAP_SECONDS:
            sessions.append(current)
            current = []
        current.append(m)
        last_ts = m["ts"]
    if current:
        sessions.append(current)
    return sessions[-limit:]
