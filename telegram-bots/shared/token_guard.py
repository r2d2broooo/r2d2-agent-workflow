"""Runaway-spend guard for cloud model calls (operator 2026-11-03).

Four layers, all fed by the real per-call counts in shared/token_usage.py:

0. Limit stop. A usage, session, weekly or rate limit (or exhausted credits) from Claude, Codex
   or Grok holds EVERY call to that provider until the reset the CLI printed, else the meter's
   reset, else 30 minutes (5 for a rate limit). The next call after that probes again.
1. Loop breaker. FAIL_STREAK failed calls in a row from one scheduled source (paper, lab,
   diagnose, ...) pause that source for that provider. This would have stopped the 240 failed
   Codex calls on 10-28 and the 232 cancelled Grok calls on 11-02.
2. Call and size ceilings. More calls per hour than a scheduled source can need pauses it.
   One chat reply above REPLY_ALERT_EFFECTIVE tokens raises an alert. Chats above
   INTERACTIVE_HOURLY_EFFECTIVE in an hour pause that provider's chats for INTERACTIVE_PAUSE_S.
3. Burn watch (`python -m shared.token_guard watch`, token-watch.timer every 10 minutes).
   If a provider's weekly meter rises faster than BURN[provider] points in an hour, the
   operator is alerted; at the trip level its chats and helper jobs pause for an hour. Paper runs
   are never paused by it, so a busy chat never skips a trading slot; they stop only at the
   provider's limit (layer 0).

A paused call costs nothing: the runner returns "<provider> paused by token guard: why" and the
callers treat it like a usage limit. Pauses expire on their own.
Status / clear: python -m shared.token_guard status | clear [provider]
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import threading
import time
from pathlib import Path

from shared import token_usage

log = logging.getLogger(__name__)

_DEFAULT_STATE = Path(__file__).resolve().parent.parent / "data" / "token_guard.json"
STATE = Path(os.environ.get("R2D2_TOKEN_GUARD") or _DEFAULT_STATE)
MARKER = "paused by token guard"

FAIL_STREAK = 5
FAIL_PAUSE_S = 45 * 60
CALLS_PER_HOUR = {"paper": 90, "weekend": 12, "lab": 60, "diagnose": 6, "healer": 6,
                  "reports": 30, "cloud-supervisor": 30}
DEFAULT_SCHEDULED_CALLS = 30
CALL_PAUSE_S = 60 * 60
REPLY_ALERT_EFFECTIVE = 1_000_000
REPLY_ALERT_STEPS = 60
INTERACTIVE_HOURLY_EFFECTIVE = 6_000_000
INTERACTIVE_PAUSE_S = 30 * 60
# Weekly-meter points per hour: (alert, pause chats and helpers). Grok's full paper review
# alone is about 8 points, so its levels sit higher.
BURN = {"codex": (6.0, 12.0), "claude": (6.0, 12.0), "grok": (12.0, 20.0)}
BURN_PAUSE_S = 60 * 60
ALERT_EVERY_S = 3 * 3600
SAMPLE_KEEP_S = 26 * 3600


@contextlib.contextmanager
def _state():
    path = Path(os.environ.get("R2D2_TOKEN_GUARD") or STATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            data = {}
        data.setdefault("pauses", {})
        data.setdefault("alerts", {})
        data.setdefault("samples", {})
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)


def _keys(provider: str, source: str) -> list[str]:
    keys = [f"{provider}:*all", f"{provider}:{source}"]
    if token_usage.interactive(source):
        keys.append(f"{provider}:*chats")
    if not source.startswith(("paper", "weekend")):
        keys.append(f"{provider}:*helpers")
    return keys


def check(provider: str, source: str, now: float | None = None) -> str | None:
    """The reason this call is paused, or None. Never raises."""
    now = time.time() if now is None else now
    try:
        with _state() as data:
            pauses = data["pauses"]
            for key in [k for k, p in pauses.items() if float(p.get("until") or 0) <= now]:
                pauses.pop(key)
            for key in _keys(provider, source):
                if key in pauses:
                    p = pauses[key]
                    left = max(1, int((float(p["until"]) - now) // 60))
                    return f"{p.get('why')} (about {left} min left)"
    except Exception:
        log.exception("token guard check failed")
    return None


def refusal(provider: str, reason: str) -> str:
    return f"{provider} {MARKER}: {reason}. Nothing was sent to the model."


def _pause(data: dict, key: str, seconds: float, why: str, now: float) -> bool:
    current = data["pauses"].get(key)
    if current and float(current.get("until") or 0) > now:
        return False
    data["pauses"][key] = {"until": now + seconds, "since": now, "why": why}
    return True


def _due(data: dict, key: str, now: float) -> bool:
    if now - float(data["alerts"].get(key) or 0) < ALERT_EVERY_S:
        return False
    data["alerts"][key] = now
    return True


LIMIT_REASONS = {"credits", "credit limit", "usage limit", "session limit", "quota", "weekly limit",
                  "rate limit", "billing"}
LIMIT_DEFAULT_PAUSE_S = 30 * 60
RATE_LIMIT_PAUSE_S = 5 * 60
LIMIT_MAX_PAUSE_S = 8 * 86400
LONDON = "Europe/London"
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                       "oct", "nov", "dec"), 1)}


def limit_reason(error: str) -> str | None:
    """The provider-limit kind in an error text (usage, session, weekly, rate, credits), or None."""
    if not error or MARKER in error:
        return None
    from shared.supervisor import _capacity_reason
    reason = _capacity_reason(error)
    return reason if reason in LIMIT_REASONS else None


def _clock(hour: int, minute: int, ampm: str | None) -> tuple[int, int]:
    if ampm:
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    return hour, minute


def reset_from_text(error: str, now: float) -> float | None:
    """Reset time printed by the CLI: Codex "try again at Nov 4th, 2026 3:45 PM",
    Claude "resets 6:30pm (Europe/London)" or "resets Nov 8, 8am"."""
    import datetime as dt
    import re
    from zoneinfo import ZoneInfo
    text = error or ""
    m = re.search(r"try again at ([A-Za-z]{3})[a-z]* (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}),? (\d{1,2}):(\d{2})\s*([AP]M)?",
                  text, re.I)
    if m and m.group(1).lower() in _MONTHS:
        hour, minute = _clock(int(m.group(4)), int(m.group(5)), m.group(6))
        when = dt.datetime(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)), hour, minute,
                           tzinfo=ZoneInfo(LONDON))
        return when.timestamp()
    m = re.search(r"resets\s+(?:([A-Za-z]{3})[a-z]* (\d{1,2}),?\s+)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?"
                  r"(?:\s*\(([^)]+)\))?", text, re.I)
    if m and (m.group(5) or m.group(4)):
        try:
            zone = ZoneInfo(m.group(6) or LONDON)
        except Exception:
            zone = ZoneInfo(LONDON)
        hour, minute = _clock(int(m.group(3)), int(m.group(4) or 0), m.group(5))
        base = dt.datetime.fromtimestamp(now, zone)
        if m.group(1) and m.group(1).lower() in _MONTHS:
            when = base.replace(month=_MONTHS[m.group(1).lower()], day=int(m.group(2)), hour=hour,
                                minute=minute, second=0, microsecond=0)
            if when.timestamp() < now - 86400:
                when = when.replace(year=when.year + 1)
        else:
            when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when.timestamp() <= now:
                when += dt.timedelta(days=1)
        return when.timestamp()
    return None


def _reset_from_meter(provider: str) -> float | None:
    """When the CLI printed no time: the meter's reset for whichever window is spent."""
    import datetime as dt
    try:
        from shared import run_budget
        account = run_budget.usage().get(provider) or {}
    except Exception:
        return None
    parse = lambda v: dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    try:
        if account.get("weekly_used", 0) >= 99 and account.get("resets_at"):
            return parse(account["resets_at"])
        if account.get("short_remaining") is not None and account["short_remaining"] <= 1 and account.get("short_resets_at"):
            return parse(account["short_resets_at"])
    except (TypeError, ValueError):
        return None
    return None


def _limit_pause(provider: str, error: str, reason: str, now: float) -> tuple[float, str]:
    """(seconds to pause every call to this provider, when it ends as text)."""
    import datetime as dt
    until = reset_from_text(error, now) or _reset_from_meter(provider)
    if not until or until <= now:
        until = now + (RATE_LIMIT_PAUSE_S if reason == "rate limit" else LIMIT_DEFAULT_PAUSE_S)
    seconds = min(LIMIT_MAX_PAUSE_S, max(60.0, until - now))
    from zoneinfo import ZoneInfo
    return seconds, dt.datetime.fromtimestamp(now + seconds, ZoneInfo(LONDON)).strftime("%a %d %b %H:%M UK")


def after_call(entry: dict, now: float | None = None) -> list[str]:
    """Apply the per-call rules to a just-logged call. Returns the alerts it raised."""
    now = time.time() if now is None else now
    provider, source = entry.get("provider") or "?", entry.get("source") or "?"
    alerts: list[str] = []
    # Any provider limit stops EVERY call to that provider (paper, chats, Lab, helpers) until
    # the printed or metered reset; nothing more is sent into a limit (operator 2026-11-03).
    limit = None if entry.get("ok") else limit_reason(str(entry.get("error") or ""))
    pause = _limit_pause(provider, str(entry.get("error") or ""), limit, now) if limit else None
    recent = [r for r in token_usage.read(now - 3600)
              if r.get("provider") == provider and r.get("source") == source]
    with _state() as data:
        if pause:
            seconds, until_text = pause
            why = f"{provider} hit its {limit} (until {until_text})"
            if _pause(data, f"{provider}:*all", seconds, why, now) and _due(data, f"limit:{provider}", now):
                alerts.append(f"⏸ {provider} hit its {limit}. Every {provider} call (paper, chats, Lab, helpers) "
                              f"is held until {until_text}; nothing is sent into the limit. "
                              f"Clear early: python -m shared.token_guard clear {provider}")
        if not token_usage.interactive(source):
            tail = recent[-FAIL_STREAK:]
            if len(tail) == FAIL_STREAK and not any(r.get("ok") for r in tail):
                why = f"{FAIL_STREAK} failed {source} calls in a row (last: {str(tail[-1].get('error') or 'no answer')[:120]})"
                if _pause(data, f"{provider}:{source}", FAIL_PAUSE_S, why, now):
                    alerts.append(f"🛑 Token guard paused {provider} {source} for {FAIL_PAUSE_S // 60} min: {why}.")
            cap = CALLS_PER_HOUR.get(source.split(":")[0], DEFAULT_SCHEDULED_CALLS)
            if len(recent) > cap:
                why = f"{len(recent)} {source} calls in the last hour (limit {cap})"
                if _pause(data, f"{provider}:{source}", CALL_PAUSE_S, why, now):
                    alerts.append(f"🛑 Token guard paused {provider} {source} for {CALL_PAUSE_S // 60} min: {why}.")
        else:
            size, steps = int(entry.get("effective") or 0), int(entry.get("steps") or 0)
            if (size > REPLY_ALERT_EFFECTIVE or steps > REPLY_ALERT_STEPS) and _due(data, f"reply:{provider}", now):
                alerts.append(f"⚠️ Token guard: one {provider} {source} reply used {size:,} effective tokens "
                              f"over {steps} tool steps (context ~{int(entry.get('context') or 0):,}). "
                              "A long session or a long tool loop costs this much; start a fresh task if it was not needed.")
            hourly = sum(int(r.get("effective") or 0) for r in token_usage.read(now - 3600)
                         if r.get("provider") == provider and token_usage.interactive(r.get("source") or ""))
            if hourly > INTERACTIVE_HOURLY_EFFECTIVE:
                why = f"chats used {hourly:,} effective tokens in the last hour (limit {INTERACTIVE_HOURLY_EFFECTIVE:,})"
                if _pause(data, f"{provider}:*chats", INTERACTIVE_PAUSE_S, why, now):
                    alerts.append(f"🛑 Token guard paused {provider} chats for {INTERACTIVE_PAUSE_S // 60} min: {why}. "
                                  "Clear early: python -m shared.token_guard clear " + provider)
    for text in alerts:
        _send(text)
    return alerts


def _terminal_note(provider: str, now: float) -> str:
    """Who used the provider in the last hour: terminals vs bot runs, from the token log."""
    rows = [r for r in token_usage.read(now - 3600) if r.get("provider") == provider]
    term = sum(int(r.get("effective") or 0) for r in rows if str(r.get("source") or "").startswith("terminal:"))
    bots = sum(int(r.get("effective") or 0) for r in rows) - term
    if not rows:
        return ""
    return f" Last hour: terminals {term:,}, bot runs {bots:,} effective tokens."


def watch(accounts: dict | None = None, now: float | None = None) -> list[str]:
    """Sample each weekly meter and act on its rise over the last hour."""
    now = time.time() if now is None else now
    if accounts is None:
        from shared import run_budget
        accounts = run_budget.usage()
    from shared.run_budget import same_window
    alerts: list[str] = []
    with _state() as data:
        for provider, account in (accounts or {}).items():
            if provider not in BURN:
                continue
            samples = [s for s in data["samples"].get(provider, []) if now - s[0] <= SAMPLE_KEEP_S]
            samples.append([now, float(account["weekly_used"]), account.get("resets_at") or ""])
            data["samples"][provider] = samples
            hour_ago = [s for s in samples if now - s[0] <= 3600 + 300 and same_window(s[2], samples[-1][2])]
            if len(hour_ago) < 2:
                continue
            rise = hour_ago[-1][1] - hour_ago[0][1]
            span = (hour_ago[-1][0] - hour_ago[0][0]) / 60
            alert_at, pause_at = BURN[provider]
            if rise >= pause_at:
                why = f"weekly meter rose {rise:.0f} points in {span:.0f} min"
                if _pause(data, f"{provider}:*helpers", BURN_PAUSE_S, why, now):
                    alerts.append(f"🛑 Token guard: {provider} {why} (now {account['weekly_used']:.0f}%). "
                                  f"Chats and helper jobs are paused for {BURN_PAUSE_S // 60} min; paper runs "
                                  "keep going until the limit. Terminal sessions are not paused - check them."
                                  + _terminal_note(provider, now))
            elif rise >= alert_at and _due(data, f"burn:{provider}", now):
                alerts.append(f"⚠️ Token guard: {provider} weekly meter rose {rise:.0f} points in {span:.0f} min "
                              f"(now {account['weekly_used']:.0f}%)." + _terminal_note(provider, now))
    # Report project anomalies through the existing watch/notification channel. No new timer
    # or pause policy. A report failure must not affect account guard decisions.
    try:
        from shared import project_observability
        for alert in project_observability.usage_report()["alerts"]:
            with _state() as data:
                key = f"project-usage:{alert['project']}:{alert['date']}"
                due = key not in data["alerts"]
                if due:
                    data["alerts"][key] = now
            if due:
                alerts.append("⚠️ " + alert["message"])
    except Exception:
        pass
    for text in alerts:
        _send(text)
    return alerts


def clear(provider: str | None = None) -> int:
    with _state() as data:
        keys = [k for k in data["pauses"] if not provider or k.startswith(provider + ":")]
        for key in keys:
            data["pauses"].pop(key)
    return len(keys)


def status(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with _state() as data:
        return {k: {**p, "minutes_left": int((float(p["until"]) - now) // 60)}
                for k, p in data["pauses"].items() if float(p.get("until") or 0) > now}


def _send(text: str) -> None:
    """Operator alert in the Telegram group; a separate thread so a running loop is not needed."""
    log.warning(text)

    def run():
        import asyncio
        from shared import telegram_sender
        with contextlib.suppress(Exception):
            asyncio.run(telegram_sender.send_message("personal", text))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=15)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Token guard")
    ap.add_argument("command", choices=("watch", "status", "clear"))
    ap.add_argument("provider", nargs="?")
    a = ap.parse_args(argv)
    if a.command == "watch":
        # Terminal sessions first, so the burn check below can say who used the meter.
        try:
            from shared import terminal_usage
            terminal_usage.scan()
        except Exception as exc:
            print(f"terminal scan failed: {type(exc).__name__}: {exc}")
        # Terminal conversations into short- and long-term memory (no model calls).
        try:
            from shared import terminal_memory
            terminal_memory.save()
        except Exception as exc:
            print(f"terminal memory failed: {type(exc).__name__}: {exc}")
        try:
            alerts = watch()
        except Exception as exc:
            print(f"usage unavailable: {type(exc).__name__}: {exc}")
            return 0
        print("\n".join(alerts) or "burn rates normal")
    elif a.command == "clear":
        print(f"cleared {clear(a.provider)} pause(s)")
    else:
        print(json.dumps(status(), indent=1) or "{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
