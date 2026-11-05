"""Persistent (sqlite-backed) conversation state for the local LLM.

Lets the local model's conversation survive process restarts and be shared
across the dashboard and Telegram processes, keyed by a canonical chat_id
(see shared.group_chat).
"""

import json
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "chat_log.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_history (
            chat_id INTEGER NOT NULL,
            idx INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL,
            PRIMARY KEY (chat_id, idx)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_pending_action (
            chat_id INTEGER PRIMARY KEY,
            tool_call_json TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            ts REAL NOT NULL
        )
        """
    )
    return conn


def get_history(chat_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT role, content FROM llm_history WHERE chat_id = ? ORDER BY idx ASC",
            (chat_id,),
        ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows]
    finally:
        conn.close()


def append_message(chat_id: int, role: str, content: str) -> None:
    conn = _connect()
    try:
        row = conn.execute("SELECT MAX(idx) FROM llm_history WHERE chat_id = ?", (chat_id,)).fetchone()
        next_idx = (row[0] + 1) if row and row[0] is not None else 0
        conn.execute(
            "INSERT INTO llm_history (chat_id, idx, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            (chat_id, next_idx, role, content, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def replace_history(chat_id: int, messages: list[dict]) -> None:
    """Overwrite the stored history for chat_id with `messages` ({"role","content"})."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM llm_history WHERE chat_id = ?", (chat_id,))
        now = time.time()
        conn.executemany(
            "INSERT INTO llm_history (chat_id, idx, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            [(chat_id, i, m["role"], m["content"], now) for i, m in enumerate(messages)],
        )
        conn.commit()
    finally:
        conn.close()


def clear_history(chat_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM llm_history WHERE chat_id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def set_pending(chat_id: int, tool_call: dict, messages: list[dict]) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO llm_pending_action (chat_id, tool_call_json, messages_json, ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                tool_call_json = excluded.tool_call_json,
                messages_json = excluded.messages_json,
                ts = excluded.ts
            """,
            (chat_id, json.dumps(tool_call), json.dumps(messages), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending(chat_id: int) -> tuple[dict, list[dict]] | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT tool_call_json, messages_json FROM llm_pending_action WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row[0]), json.loads(row[1])
    finally:
        conn.close()


def clear_pending(chat_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM llm_pending_action WHERE chat_id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()
