"""Weekly and short-window meter readings for the cloud accounts, from `paseo usage`.

The 50%-of-the-week run budget that used this module was removed (operator 2026-11-03): daily
runs, weekend reviews and Lab rounds run until the provider's own limit, and shared/token_guard
then holds that provider until it resets. These readings still feed the token guard's burn watch
and limit resets and model_choices.pick_helper.
"""
from __future__ import annotations

import datetime as dt

WINDOW_DRIFT_S = 3 * 3600
PROVIDERS = ("claude", "codex", "grok")


def _accounts(payload) -> dict[str, dict]:
    """{provider: {weekly_used, resets_at, short_remaining, short_resets_at}} from `paseo usage ls --json`."""
    out = {}
    for source in payload if isinstance(payload, list) else []:
        if not isinstance(source, dict):
            continue
        provider = str(source.get("sourceId") or source.get("sourceLabel") or "").strip().lower()
        report = source.get("report") if isinstance(source.get("report"), dict) else {}
        if provider not in PROVIDERS or report.get("status") != "available":
            continue
        weekly, short = None, []
        for window in report.get("windows") or []:
            if not isinstance(window, dict) or not isinstance(window.get("remainingPct"), (int, float)):
                continue
            if str(window.get("id") or window.get("label") or "").lower() == "weekly":
                weekly = window
            else:
                short.append((float(window["remainingPct"]), str(window.get("resetsAt") or "")))
        if weekly is None:
            continue
        tightest = min(short) if short else None
        out[provider] = {"weekly_used": 100.0 - float(weekly["remainingPct"]),
                         "resets_at": str(weekly.get("resetsAt") or ""),
                         "short_remaining": tightest[0] if tightest else None,
                         "short_resets_at": tightest[1] if tightest else ""}
    return out


def usage() -> dict[str, dict]:
    from shared import desk_quota
    return _accounts(desk_quota.fetch_paseo())


def same_window(a: str, b: str) -> bool:
    """Providers report the reset time with drift (07:59 on one read, 08:00:00.3 on the next)."""
    if a == b:
        return True
    try:
        parse = lambda v: dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        return abs(parse(a) - parse(b)) < WINDOW_DRIFT_S
    except (TypeError, ValueError):
        return False
