"""Durable project conversations, independent native sessions, and bounded team rounds."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import agent_registry, project_observability, token_usage
from .secret_redaction import redact_secrets

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent / "r2d2-agents"
STATE_DIR = Path(os.environ.get("R2D2_AGENT_STATE_DIR", ROOT.parent / ".state/agent-projects"))
TASKS: set[asyncio.Task] = set()


@contextmanager
def _db():
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = STATE_DIR / "sessions.sqlite3"
    conn = sqlite3.connect(path, timeout=20)
    os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, provider TEXT NOT NULL,
        discussion INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'idle',
        created REAL NOT NULL, updated REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, provider TEXT NOT NULL,
        role TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL, round INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS session_runs (
        id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, provider TEXT NOT NULL,
        project TEXT NOT NULL, metadata TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS session_projects (
        session_id TEXT PRIMARY KEY, project TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,id);
    """)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _id(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Invalid session id")
    return value


def providers():
    rows = {a["provider"]: a for a in agent_registry.status() if a.get("role") == "supervisor"}
    return [{"id": key, "label": rows.get(key, {}).get("label", key.title()),
             "available": bool(rows.get(key, {}).get("available")), "setup": ""}
            for key in ("claude", "codex", "grok")]


def list_sessions():
    with _db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT s.id,s.title,s.provider,s.discussion,s.status,s.created,s.updated, "
            "COALESCE(p.project,'telegram-bots') AS project FROM sessions s "
            "LEFT JOIN session_projects p ON p.session_id=s.id ORDER BY s.updated DESC")]


def create(title: str, provider: str, discussion=False, project=ROOT.parent.name):
    if provider not in {"claude", "codex", "grok", "team"}:
        raise ValueError("Choose Claude, Codex, Grok or the team")
    title = redact_secrets(str(title)).strip()[:100]
    if not title:
        raise ValueError("A session name is required")
    project_observability.project_path(project)
    sid, now = uuid.uuid4().hex, time.time()
    with _db() as conn:
        conn.execute("INSERT INTO sessions(id,title,provider,discussion,created,updated) VALUES(?,?,?,?,?,?)",
                     (sid, title, provider, int(bool(discussion)), now, now))
        conn.execute("INSERT INTO session_projects VALUES(?,?)", (sid, project))
    return get(sid)


def get(sid):
    with _db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (_id(sid),)).fetchone()
        if row is None:
            raise KeyError("Unknown session")
        out = dict(row)
        project = conn.execute("SELECT project FROM session_projects WHERE session_id=?", (sid,)).fetchone()
        out["project"] = project[0] if project else ROOT.parent.name
        import json
        out["runs"] = [dict(provider=r["provider"], project=r["project"], **json.loads(r["metadata"]))
                       for r in conn.execute("SELECT * FROM session_runs WHERE session_id=? ORDER BY id DESC LIMIT 20", (sid,))]
        out["messages"] = [dict(r) for r in conn.execute(
            "SELECT id,provider,role,text,ts,round FROM messages WHERE session_id=? ORDER BY id", (sid,))]
        return out


def _message(sid, provider, role, text, round_=0):
    with _db() as conn:
        conn.execute("INSERT INTO messages(session_id,provider,role,text,ts,round) VALUES(?,?,?,?,?,?)",
                     (sid, provider, role, redact_secrets(text), time.time(), round_))
        conn.execute("UPDATE sessions SET updated=? WHERE id=?", (time.time(), sid))


def _status(sid, status):
    with _db() as conn:
        conn.execute("UPDATE sessions SET status=?, updated=? WHERE id=?", (status, time.time(), sid))


def _claim(sid, expected):
    with _db() as conn:
        count = conn.execute("UPDATE sessions SET status='running',updated=? WHERE id=? AND status=?",
                             (time.time(), _id(sid), expected)).rowcount
        if count != 1:
            raise ValueError("This session is busy or has a pending command to review")


def _cwd(sid, provider):
    path = STATE_DIR / "workdirs" / _id(sid) / provider
    path.mkdir(parents=True, exist_ok=True)
    instructions = path / "AGENTS.md"
    instructions.write_text((PROJECT_ROOT / "AGENTS.md").read_text())
    return str(path)


def _context():
    return (PROJECT_ROOT / "AGENTS.md").read_text()


async def _call(sid, provider, prompt):
    cwd = _cwd(sid, provider)
    if provider not in {"claude", "codex", "grok"}:
        raise ValueError("Unknown project-session provider")
    from .supervisor import run_named_supervisor
    project = get(sid)["project"]
    root = project_observability.project_path(project)
    before, started = project_observability.snapshot(root), time.time()
    revision = project_observability.instruction_revision()
    try:
        with token_usage.project(str(root), sid):
            reply = await run_named_supervisor(
                provider, f"CURRENT PROJECT CONTRACT (revision {revision}):\n{_context()}\n"
                f"Assigned repository: {root}\n\n{prompt}",
                cwd, str(ROOT / f"prompts/{provider}/system_prompt.md"))
    finally:
        import json
        metadata = project_observability.provenance(root, before, started)
        metadata["instruction_revision"] = revision
        calls = [r for r in token_usage.read(started - 0.001) if r.get("project_session") == sid
                 and r.get("provider") == provider and float(r.get("ts") or 0) <= metadata["finished"] + 0.001]
        metadata["usage"] = {"calls": len(calls),
                             "effective": sum(int(r.get("effective") or 0) for r in calls),
                             "models": sorted({r.get("model") or "unreported" for r in calls}),
                             "efforts": sorted({r.get("effort") or "unreported" for r in calls})}
        with _db() as conn:
            conn.execute("INSERT INTO session_runs(session_id,provider,project,metadata) VALUES(?,?,?,?)",
                         (sid, provider, project, json.dumps(metadata)))
    from .supervisor import classify_provider_reply
    if not classify_provider_reply(provider, reply)["ok"]:
        raise RuntimeError(reply)
    return reply


async def _work(sid, text):
    try:
        session = get(sid)
        if session["provider"] != "team":
            reply = await _call(sid, session["provider"], text)
            _message(sid, session["provider"], "assistant", reply)
        else:
            # Freeze a common transcript before either independent response is generated.
            transcript = "\n".join(f'{m["provider"]}: {m["text"]}' for m in session["messages"])
            if len(transcript) > 250_000:
                raise ValueError("Team history is too large; create a new session to preserve complete context")
            async def member(provider, prompt, round_):
                try:
                    if not next(p["available"] for p in providers() if p["id"] == provider):
                        raise ValueError(f"{provider.title()} is not configured or available")
                    reply = await _call(sid, provider, prompt)
                    _message(sid, provider, "assistant", reply, round_)
                    return provider, reply
                except Exception as exc:
                    _message(sid, provider, "error", str(exc), round_)
                    return provider, None
            instruction = ("Joint project discussion requested by the operator. Analyse and propose changes only; "
                           "do not edit files or execute commands in the team room. Keep your own identity. "
                           "Give your independent answer to the latest user message using this shared transcript:\n")
            first = await asyncio.gather(*(member(p, instruction + transcript, 1)
                                           for p in ("claude", "codex", "grok")))
            if session["discussion"]:
                evidence = "\n\n".join(f"{p}: {r}" for p, r in first if r is not None)
                await asyncio.gather(*(member(p, "Review these independent answers. State concrete agreements, "
                    "disagreements and one next step. Discussion only; no edits or commands.\n\n" + evidence, 2)
                    for p, r in first if r is not None))
        _status(sid, "idle")
    except asyncio.CancelledError:
        _status(sid, "interrupted")
        raise
    except Exception as exc:
        _message(sid, "system", "error", str(exc))
        _status(sid, "idle")


def start(sid, text):
    session = get(sid)
    text = redact_secrets(str(text)).strip()
    if not text or len(text) > 60000:
        raise ValueError("Message must contain 1–60,000 characters")
    _claim(sid, "idle")
    _message(sid, "user", "user", text)
    _launch(_work(sid, text))


def _launch(coro):
    task = asyncio.create_task(coro)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def recover():
    """Called once on dashboard startup; never replay an interrupted provider turn."""
    with _db() as conn:
        conn.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")


def acknowledge(sid):
    _claim(sid, "interrupted")
    _status(sid, "idle")
