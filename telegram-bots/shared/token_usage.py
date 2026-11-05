"""Real token use of every cloud model call (operator 2026-11-03).

Each Claude, Codex and Grok CLI call appends one line to data/token_usage.jsonl with the
provider's own counts: uncached input, cache reads, cache writes, output (reasoning included),
model rounds and tool steps. Codex calls that keep a native session also carry the weekly and
five-hour meter readings from the session file. "effective" weighs cache reads at a tenth,
which is roughly how the subscription meters count them.

The source label says what made the call (paper, telegram-group, lab, diagnose, ...). A caller
can set it with `with token_usage.source("lab"):`, otherwise it comes from the working folder.

Interactive terminals (Claude Code, Codex TUI, Grok TUI) are added by shared.terminal_usage,
one row per session and hour with source "terminal:<tool>" (token-watch scans every 10 min).

Report: python -m shared.token_usage [--hours 24]
"""
from __future__ import annotations

import contextlib
import contextvars
import glob
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_LOG = Path(__file__).resolve().parent.parent / "data" / "token_usage.jsonl"
LOG = Path(os.environ.get("R2D2_TOKEN_LOG") or _DEFAULT_LOG)
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CACHE_READ_WEIGHT = 0.1

_PROJECT = contextvars.ContextVar("token_project", default={})


@contextlib.contextmanager
def project(root: str, session: str):
    handle = _PROJECT.set({"project_root": root, "project_session": session})
    try:
        yield
    finally:
        _PROJECT.reset(handle)


_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar("token_source", default="")


@contextlib.contextmanager
def source(label: str):
    """Label every model call made inside this block (it follows asyncio tasks)."""
    token = _SOURCE.set(label)
    try:
        yield
    finally:
        _SOURCE.reset(token)


def classify(cwd: str, isolated: bool = False) -> str:
    explicit = _SOURCE.get()
    if explicit:
        return explicit
    path = os.path.realpath(os.path.expanduser(cwd or ""))
    name = os.path.basename(path)
    if "/cloud-workdir/" in path + "/":
        if name.endswith("-repair"):
            return "healer"
        if "-health-" in name:
            return "diagnose"
        return "paper" if isolated else "cloud-supervisor"
    if "/claude-workdir/" in path or "/shared-claude/" in path:
        if "group" in name:
            return "telegram-group"
        if "personal" in name:
            return "telegram-personal"
        return f"chat:{name}"
    if "/claude-sessions/" in path or "reports" in name:
        return "reports"
    return f"other:{name or '?'}"


def interactive(source_label: str) -> bool:
    return source_label.startswith(("telegram-", "chat:", "other:"))


def _num(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize(raw: dict | None, *, input_includes_cache: bool) -> dict:
    """One shape for every provider: input is UNCACHED input only."""
    raw = raw if isinstance(raw, dict) else {}
    pick = lambda *keys: next((_num(raw[k]) for k in keys if k in raw), 0)
    cached = pick("cache_read_input_tokens", "cacheReadInputTokens", "cached_input_tokens",
                  "cachedInputTokens", "cachedReadTokens", "cacheReadTokens")
    written = pick("cache_creation_input_tokens", "cacheCreationInputTokens", "cacheWriteTokens",
                   "cache_write_tokens")
    total_in = pick("input_tokens", "inputTokens", "prompt_tokens", "promptTokens")
    uncached = max(0, total_in - cached) if input_includes_cache else total_in
    return {"input": uncached, "cached": cached, "cache_write": written,
            "output": pick("output_tokens", "outputTokens", "completion_tokens"),
            "reasoning": pick("reasoning_output_tokens", "reasoning_tokens", "reasoningTokens",
                              "thoughtTokens", "thinking_tokens")}


def effective(usage: dict) -> int:
    return int(usage.get("input", 0) + usage.get("cache_write", 0) + usage.get("output", 0)
               + CACHE_READ_WEIGHT * usage.get("cached", 0))


def context_size(usage: dict) -> int:
    """Rough size of the conversation one model round re-sends."""
    rounds = max(1, int(usage.get("turns") or 0), int(usage.get("steps") or 0) + 1)
    return int((usage.get("input", 0) + usage.get("cached", 0) + usage.get("cache_write", 0)) / rounds)


# ---- provider parsers ------------------------------------------------------------------------

def codex_usage(stdout: str) -> dict | None:
    """Sum `turn.completed` usage from `codex exec --json` and count tool steps."""
    total = {"input": 0, "cached": 0, "cache_write": 0, "output": 0, "reasoning": 0}
    turns = steps = 0
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            turns += 1
            for key, value in normalize(event["usage"], input_includes_cache=True).items():
                total[key] += value
        elif event.get("type") == "item.completed":
            kind = str((event.get("item") or {}).get("type") or "")
            if kind and kind not in ("agent_message", "reasoning"):
                steps += 1
    if not turns:
        return None
    return {**total, "turns": turns, "steps": steps}


def codex_meter(thread_id: str | None) -> dict:
    """Weekly and five-hour readings after the call, from the native session file (if any)."""
    if not thread_id:
        return {}
    try:
        files = glob.glob(str(CODEX_SESSIONS / "*" / "*" / "*" / f"rollout-*{thread_id}.jsonl"))
        if not files:
            return {}
        path = max(files, key=os.path.getmtime)
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 262144))
            tail = fh.read().decode(errors="ignore").splitlines()
        for line in reversed(tail):
            if '"rate_limits"' not in line:
                continue
            payload = (json.loads(line).get("payload") or {})
            limits = payload.get("rate_limits") or {}
            out = {}
            if isinstance(limits.get("secondary"), dict):
                out["weekly_pct"] = limits["secondary"].get("used_percent")
            if isinstance(limits.get("primary"), dict):
                out["short_pct"] = limits["primary"].get("used_percent")
            return out
    except (OSError, ValueError):
        pass
    return {}


def claude_usage(payload: dict) -> dict | None:
    """Totals from `claude -p --output-format json` (modelUsage covers every model it used)."""
    if not isinstance(payload, dict):
        return None
    models = payload.get("modelUsage")
    if isinstance(models, dict) and models:
        total = {"input": 0, "cached": 0, "cache_write": 0, "output": 0, "reasoning": 0}
        for row in models.values():
            row = row if isinstance(row, dict) else {}
            for key, value in normalize(row, input_includes_cache=False).items():
                total[key] += value
            total["reasoning"] += _num(row.get("thinkingTokens"))
    elif isinstance(payload.get("usage"), dict):
        total = normalize(payload["usage"], input_includes_cache=False)
    else:
        return None
    iterations = (payload.get("usage") or {}).get("iterations") if isinstance(payload.get("usage"), dict) else None
    total["turns"] = _num(payload.get("num_turns")) or (len(iterations) if isinstance(iterations, list) else 0)
    total["steps"] = max(0, total["turns"] - 1)
    if isinstance(payload.get("total_cost_usd"), (int, float)):
        total["cost_usd"] = round(float(payload["total_cost_usd"]), 6)
    return total


def grok_usage(payload: dict, *, acp: bool = False) -> dict | None:
    """Headless JSON reports uncached input; ACP `_meta.usage` reports the full prompt."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("usage")
    if acp:
        raw = ((payload.get("_meta") or {}).get("usage")) or raw
    if not isinstance(raw, dict):
        return None
    total = normalize(raw, input_includes_cache=acp)
    total["turns"] = _num(payload.get("num_turns"))
    total["steps"] = max(0, total["turns"] - 1)
    if isinstance(payload.get("total_cost_usd"), (int, float)):
        total["cost_usd"] = round(float(payload["total_cost_usd"]), 6)
    return total


# ---- ledger ----------------------------------------------------------------------------------

def record(provider: str, *, cwd: str, isolated: bool, ok: bool, started: float,
           usage: dict | None, model: str | None = None, effort: str | None = None,
           prompt_chars: int = 0, reply_chars: int = 0, error: str = "",
           meter: dict | None = None) -> dict | None:
    """Append one call. Never raises: a logging fault must not break a model call."""
    try:
        label = classify(cwd, isolated)
        usage = dict(usage or {})
        entry = {"ts": round(time.time(), 3), "provider": provider, "model": model or "",
                 "source": label, "cwd": cwd, "isolated": bool(isolated), "ok": bool(ok),
                 "seconds": round(time.time() - started, 1),
                 "prompt_chars": int(prompt_chars or 0), "reply_chars": int(reply_chars or 0),
                 "reported": bool(usage)}
        entry.update(_PROJECT.get())
        if effort:
            entry["effort"] = str(effort)[:40]
        if usage:
            entry.update({k: usage.get(k, 0) for k in ("input", "cached", "cache_write", "output",
                                                        "reasoning", "turns", "steps")})
            if "cost_usd" in usage:
                entry["cost_usd"] = usage["cost_usd"]
            entry["effective"] = effective(usage)
            entry["context"] = context_size(usage)
        else:
            # No counts came back (failed start, refused call): keep a prompt-size estimate.
            entry["estimated_input"] = int((prompt_chars or 0) / 4)
            entry["effective"] = 0
        if meter:
            entry.update({k: v for k, v in meter.items() if v is not None})
        if error:
            entry["error"] = str(error)[:300]
        path = Path(os.environ.get("R2D2_TOKEN_LOG") or LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        log.exception("token usage record failed for %s", provider)
        return None
    try:
        from shared import token_guard
        token_guard.after_call(entry)
    except Exception:
        log.exception("token guard check failed for %s", provider)
    return entry


TAIL_BYTES = 4 * 1024 * 1024


def _parse(lines, since: float) -> list[dict]:
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and float(row.get("ts") or 0) >= since:
            rows.append(row)
    return rows


def read(since: float = 0.0) -> list[dict]:
    """Calls logged at or after `since`. Recent windows read only the end of the file."""
    path = Path(os.environ.get("R2D2_TOKEN_LOG") or LOG)
    try:
        size = path.stat().st_size
        if since and size > TAIL_BYTES:
            with open(path, "rb") as fh:
                fh.seek(size - TAIL_BYTES)
                lines = fh.read().decode(errors="ignore").splitlines()[1:]
            first = _parse(lines[:1], 0.0)
            if first and float(first[0].get("ts") or 0) < since:
                return _parse(lines, since)
        with open(path) as fh:
            return _parse(fh, since)
    except OSError:
        return []


def calls(row: dict) -> int:
    """Model calls in one row: a terminal row sums a session's calls over up to an hour."""
    if str(row.get("source") or "").startswith("terminal:"):
        return max(1, int(row.get("turns") or 1))
    return 1


def summary(hours: float = 24.0) -> list[dict]:
    rows = read(time.time() - hours * 3600)
    groups: dict[tuple, dict] = {}
    for row in rows:
        key = (row.get("provider"), row.get("source"))
        g = groups.setdefault(key, {"provider": key[0], "source": key[1], "calls": 0, "failed": 0,
                                    "input": 0, "cached": 0, "cache_write": 0, "output": 0,
                                    "effective": 0, "biggest": 0})
        g["calls"] += calls(row)
        g["failed"] += 0 if row.get("ok") else 1
        for k in ("input", "cached", "cache_write", "output", "effective"):
            g[k] += int(row.get(k) or 0)
        g["biggest"] = max(g["biggest"], int(row.get("effective") or 0))
    return sorted(groups.values(), key=lambda g: -g["effective"])


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Real token use per provider and source")
    ap.add_argument("--hours", type=float, default=24.0)
    a = ap.parse_args(argv)
    rows = summary(a.hours)
    if not rows:
        print(f"no model calls logged in the last {a.hours:g} hours")
        return 0
    print(f"{'provider':8} {'source':22} {'calls':>5} {'fail':>4} {'uncached':>10} {'cache read':>11} "
          f"{'output':>9} {'effective':>10} {'largest':>9}")
    for g in rows:
        print(f"{g['provider']:8} {g['source'][:22]:22} {g['calls']:5} {g['failed']:4} {g['input']:10,} "
              f"{g['cached']:11,} {g['output']:9,} {g['effective']:10,} {g['biggest']:9,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
