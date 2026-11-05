"""Token use of the interactive terminals (operator 2026-11-04).

Claude Code, the Codex TUI and the Grok TUI do not go through the bot runners, so their calls
never reached data/token_usage.jsonl, yet they were most of the weekly meters. This scanner
reads their own session files and appends one row per session and hour to the same log, with
source "terminal:claude-code", "terminal:codex" or "terminal:grok".

Bot calls that also leave session files are skipped (they are logged by their runners):
Claude `-p` runs (entrypoint sdk-cli), `codex exec` runs, and any session whose working folder
belongs to a bot (cloud-workdir, claude-workdir, ...) or a temp folder.

token-watch runs scan() every 10 minutes. By hand: python -m shared.terminal_usage [--since-days N]
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import token_usage

STATE = Path(os.environ.get("R2D2_TERMINAL_STATE")
             or Path(__file__).resolve().parent.parent / "data" / "terminal_usage_state.json")
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
GROK_SESSIONS = Path.home() / ".grok" / "sessions"
# The first scan backfills this far so the current weekly windows are complete.
BACKFILL_DAYS = 8
BUCKET_S = 3600
BOT_FOLDERS = ("claude-workdir", "cloud-workdir", "shared-claude", "claude-sessions")


def _ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _terminal_cwd(cwd: str) -> bool:
    """A folder a person works in, not one the bots own (those calls are already logged)."""
    path = os.path.realpath(os.path.expanduser(cwd or ""))
    if path.startswith(("/tmp/", "/var/tmp/")):
        return False
    return token_usage.classify(path).startswith("other:") if cwd else True


def _lines(path: str, offset: int):
    """Complete lines after `offset`; yields (line, end offset)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        pos = offset
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            pos += len(raw)
            yield raw.decode(errors="ignore"), pos


class _Sink:
    """Sums calls per (tool, session, hour) and turns them into log rows."""

    def __init__(self):
        self.groups: dict[tuple, dict] = {}

    def add(self, tool: str, session: str, cwd: str, model: str, ts: float, *, uncached: int,
            cached: int, written: int, output: int, reasoning: int = 0, calls: int = 1,
            effort: str = "") -> None:
        effort = str(effort or "")
        key = (tool, session, int(ts // BUCKET_S), model or "", effort)
        g = self.groups.setdefault(key, {"ts": ts, "model": model, "effort": effort, "cwd": cwd,
                                         "input": 0, "cached": 0, "cache_write": 0, "output": 0,
                                         "reasoning": 0, "calls": 0, "context": 0})
        g["ts"] = max(g["ts"], ts)
        g["model"] = model or g["model"]
        g["input"] += uncached
        g["cached"] += cached
        g["cache_write"] += written
        g["output"] += output
        g["reasoning"] += reasoning
        g["calls"] += calls
        g["context"] = max(g["context"], (uncached + cached + written) // max(1, calls))

    def rows(self) -> list[dict]:
        out = []
        for (tool, session, *_rest), g in sorted(self.groups.items(), key=lambda kv: kv[1]["ts"]):
            row = {"ts": round(g["ts"], 3), "provider": {"claude-code": "claude", "codex": "codex",
                                                         "grok": "grok"}[tool],
                   "model": g["model"], "source": f"terminal:{tool}", "cwd": g["cwd"], "session": session,
                   "isolated": False, "ok": True, "seconds": 0, "prompt_chars": 0, "reply_chars": 0,
                   "reported": True, "input": g["input"], "cached": g["cached"],
                   "cache_write": g["cache_write"], "output": g["output"], "reasoning": g["reasoning"],
                   "turns": g["calls"], "steps": max(0, g["calls"] - 1), "context": g["context"]}
            if g.get("effort"):
                row["effort"] = g["effort"]
            row["effective"] = token_usage.effective(row)
            out.append(row)
        return out


def _claude(path: str, st: dict, since: float, sink: _Sink) -> None:
    seen = list(st.get("ids") or [])
    project = Path(path).relative_to(CLAUDE_PROJECTS).parts[0]
    bot_project = any(part in project for part in BOT_FOLDERS)
    session = Path(path).stem
    if "/subagents/" in path:
        session = Path(path).parent.parent.name
    for line, pos in _lines(path, int(st.get("offset") or 0)):
        st["offset"] = pos
        if '"effort"' in line and '"output_config"' in line:
            try:
                request = json.loads(line)
            except ValueError:
                request = None
            params = (request or {}).get("params") if isinstance(request, dict) else None
            config = params.get("output_config") if isinstance(params, dict) else None
            if isinstance(config, dict) and config.get("effort"):
                st["effort"] = str(config["effort"])
        if '"usage"' not in line or '"assistant"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        msg = r.get("message") or {}
        usage = msg.get("usage")
        if r.get("type") != "assistant" or not isinstance(usage, dict):
            continue
        # A terminal session may cd into a bot folder, so its start folder (the project name) decides.
        if r.get("entrypoint") == "sdk-cli" or bot_project:
            continue
        # One API response is written once per content block, each with the same usage.
        mid = f"{msg.get('id')}|{r.get('requestId')}"
        if mid in seen:
            continue
        seen = (seen + [mid])[-64:]
        ts = _ts(r.get("timestamp"))
        if ts < since or msg.get("model") == "<synthetic>":
            continue
        sink.add("claude-code", r.get("sessionId") or session, r.get("cwd") or "", msg.get("model") or "", ts,
                 uncached=int(usage.get("input_tokens") or 0),
                 cached=int(usage.get("cache_read_input_tokens") or 0),
                 written=int(usage.get("cache_creation_input_tokens") or 0),
                 output=int(usage.get("output_tokens") or 0),
                 effort=st.get("effort") or "")
    st["ids"] = seen


def _codex(path: str, st: dict, since: float, sink: _Sink) -> None:
    session = Path(path).stem[-36:]
    for line, pos in _lines(path, int(st.get("offset") or 0)):
        st["offset"] = pos
        try:
            r = json.loads(line)
        except ValueError:
            continue
        p = r.get("payload") or {}
        if r.get("type") == "session_meta":
            st["skip"] = p.get("originator") == "codex_exec" or not _terminal_cwd(p.get("cwd") or "")
            st["cwd"] = p.get("cwd") or ""
        elif r.get("type") == "turn_context":
            st["model"] = p.get("model") or st.get("model") or ""
            settings = (p.get("collaboration_mode") or {}).get("settings") or {}
            st["effort"] = str(p.get("reasoning_effort") or settings.get("reasoning_effort") or st.get("effort") or "")
        elif r.get("type") == "event_msg" and (p.get("type") == "thread_settings_applied"):
            settings = p.get("thread_settings") or {}
            if isinstance(settings, dict):
                st["model"] = settings.get("model") or st.get("model") or ""
                st["effort"] = str(settings.get("reasoning_effort") or st.get("effort") or "")
        elif r.get("type") == "event_msg" and p.get("type") == "token_count" and not st.get("skip"):
            last = (p.get("info") or {}).get("last_token_usage")
            ts = _ts(r.get("timestamp"))
            if not isinstance(last, dict) or ts < since:
                continue
            total_in, cached = int(last.get("input_tokens") or 0), int(last.get("cached_input_tokens") or 0)
            sink.add("codex", session, st.get("cwd") or "", st.get("model") or "", ts,
                     uncached=max(0, total_in - cached), cached=cached,
                     written=int(last.get("cache_write_input_tokens") or 0),
                     output=int(last.get("output_tokens") or 0),
                     reasoning=int(last.get("reasoning_output_tokens") or 0),
                     effort=st.get("effort") or "")


def _grok(path: str, st: dict, since: float, sink: _Sink) -> None:
    folder = Path(path).parent
    if "cwd" not in st or "effort" not in st:
        try:
            summary = json.loads((folder / "summary.json").read_text())
            st["cwd"] = st.get("cwd") or (summary.get("info") or {}).get("cwd") or ""
            st["effort"] = str(summary.get("reasoning_effort") or "")
            st["model"] = str(summary.get("current_model_id") or st.get("model") or "")
        except (OSError, ValueError):
            st["cwd"] = st.get("cwd") or ""
            st["effort"] = st.get("effort") or ""
    if not _terminal_cwd(st["cwd"]):
        st["offset"] = os.path.getsize(path)
        return
    for line, pos in _lines(path, int(st.get("offset") or 0)):
        st["offset"] = pos
        if '"turn_completed"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        update = (r.get("params") or {}).get("update") or {}
        usage = update.get("usage")
        ts = _ts(r.get("timestamp"))
        if update.get("sessionUpdate") != "turn_completed" or not isinstance(usage, dict) or ts < since:
            continue
        total_in, cached = int(usage.get("inputTokens") or 0), int(usage.get("cachedReadTokens") or 0)
        models = list((usage.get("modelUsage") or {}).keys())
        sink.add("grok", folder.name, st["cwd"], models[0] if models else st.get("model") or "", ts,
                 uncached=max(0, total_in - cached), cached=cached,
                 written=int(usage.get("cacheCreationTokens") or 0),
                 output=int(usage.get("outputTokens") or 0),
                 reasoning=int(usage.get("reasoningTokens") or 0),
                 calls=max(1, int(usage.get("modelCalls") or 1)),
                 effort=st.get("effort") or "")


SOURCES = (
    (_claude, lambda: glob.glob(str(CLAUDE_PROJECTS / "*" / "**" / "*.jsonl"), recursive=True)),
    (_codex, lambda: glob.glob(str(CODEX_SESSIONS / "*" / "*" / "*" / "rollout-*.jsonl"))),
    (_grok, lambda: glob.glob(str(GROK_SESSIONS / "*" / "*" / "updates.jsonl"))
     + glob.glob(str(GROK_SESSIONS.parent / "sessions-archive-*" / "*" / "*" / "updates.jsonl"))),
)


def scan(now: float | None = None, since_days: float = BACKFILL_DAYS) -> list[dict]:
    """Append rows for terminal calls made since the last scan. Never raises on a bad file."""
    now = time.time() if now is None else now
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    files = state.setdefault("files", {})
    since = float(state.get("since") or now - since_days * 86400)
    state["since"] = since
    sink = _Sink()
    for reader, finder in SOURCES:
        for path in finder():
            try:
                stat = os.stat(path)
            except OSError:
                continue
            st = files.setdefault(path, {})
            if (st.get("offset") or 0) > stat.st_size:   # rewritten file: start again
                st.clear()
            if stat.st_mtime < since or (st.get("offset") or 0) >= stat.st_size:
                continue
            try:
                reader(path, st, since, sink)
            except OSError:
                continue
    # Forget files that are gone so the state stays small.
    state["files"] = {p: s for p, s in files.items() if os.path.exists(p)}
    rows = sink.rows()
    if rows:
        log = Path(os.environ.get("R2D2_TOKEN_LOG") or token_usage.LOG)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            for row in rows:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f".{STATE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    os.replace(tmp, STATE)
    return rows


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Log interactive terminal token use")
    ap.add_argument("--since-days", type=float, default=BACKFILL_DAYS,
                    help="backfill window for the first scan only")
    a = ap.parse_args(argv)
    rows = scan(since_days=a.since_days)
    print(f"logged {len(rows)} terminal row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
