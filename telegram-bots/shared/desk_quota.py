"""Extra-work quota gate for the Paseo desk.

Reads `paseo usage ls --json` and prints remaining percent. It does not start a
model, a timer, or a trading run. Dashboard paper runs are not routed here.

Operator 2026-10-31: half of each weekly window stays reserved for dashboard
daily and weekly runs, and the other half is extra work. Extra work stops at
50% remaining. A warning starts at 60% so the cut is visible before it binds.
Review this split after 2026-11-07.

Opus 5.5 is the default commander. Grok 4.7 and GPT 6.1 Sol may also command
and distribute when the operator chooses them, or when Opus is closed and their
account is still open. A high command turn is allowed only above the warning
line. Sonnet codes and does not command.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Provisional 50/50 split. Review after one week of high paper runs.
REVIEW_ON = "2026-11-07"
DASHBOARD_RESERVE = 50
WARN_REMAINING = 60
STOP_REMAINING = DASHBOARD_RESERVE
CRITICAL_REMAINING = DASHBOARD_RESERVE

_PROVIDERS = {"claude": "claude", "codex": "codex", "grok": "grok"}
_NAMES = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}
_CLOSED = {
    "code": "closed", "make": "closed", "judge": "closed", "judge_high": "closed",
    "dispatch": "closed", "dispatch_high": "closed",
}
_PASEO = shutil.which("paseo") or str(Path.home() / ".local/bin/paseo")


def level(remaining: float | None) -> str:
    if remaining is None:
        return "unknown"
    if remaining <= CRITICAL_REMAINING:
        return "critical"
    if remaining <= STOP_REMAINING:
        return "stop"
    if remaining <= WARN_REMAINING:
        return "warn"
    return "open"


def extra_open(kind: str) -> bool:
    """Warn still allows extra work. Stop, critical, and unknown do not."""
    return kind in ("open", "warn")


def _pct(value: float | None) -> str:
    if value is None:
        return "unreadable"
    if float(value).is_integer():
        return f"{int(value)}%"
    return f"{value:.1f}%"


def _windows(report: dict) -> list[dict]:
    rows = []
    for window in report.get("windows") or []:
        if not isinstance(window, dict):
            continue
        remaining = window.get("remainingPct")
        if not isinstance(remaining, (int, float)):
            continue
        resets = window.get("resetsAt")
        rows.append({
            "label": str(window.get("label") or "window"),
            "remaining": float(remaining),
            "resets_at": resets if isinstance(resets, str) else None,
        })
    return rows


def _credits(report: dict) -> float | None:
    for balance in report.get("balances") or []:
        if not isinstance(balance, dict):
            continue
        if str(balance.get("label") or "").lower() != "credits":
            continue
        remaining = balance.get("remaining")
        if isinstance(remaining, (int, float)):
            return float(remaining)
    return None


def assess(payload) -> dict:
    """Turn a paseo usage payload into a desk decision. Account fields are dropped."""
    sources = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        nested = payload.get("sources") or payload.get("reports") or []
        sources = nested if isinstance(nested, list) else []
    rows = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        provider = _PROVIDERS.get(str(source.get("sourceLabel") or "").strip().lower())
        if not provider:
            continue
        report = source.get("report") if isinstance(source.get("report"), dict) else {}
        available = str(report.get("status") or "") == "available"
        windows = _windows(report) if available else []
        weekly = next((item for item in windows if item["label"].lower() == "weekly"), None)
        binding = min(windows, key=lambda item: item["remaining"]) if windows else None
        kind = level(binding["remaining"]) if binding else "unknown"
        rows.append({
            "provider": provider,
            "level": kind,
            "extra": "open" if extra_open(kind) else "closed",
            "windows": windows,
            "weekly_remaining": weekly["remaining"] if weekly else None,
            "weekly_resets_at": weekly["resets_at"] if weekly else None,
            "credits": _credits(report) if available else None,
        })
    return {"rows": rows, "route": _route(rows)}


def _by_provider(rows: list[dict]) -> dict[str, dict]:
    return {row["provider"]: row for row in rows}


def _open(found: dict[str, dict], provider: str) -> bool:
    row = found.get(provider)
    return bool(row) and row["extra"] == "open"


def _weekly(found: dict[str, dict], provider: str) -> float:
    row = found.get(provider) or {}
    value = row.get("weekly_remaining")
    if isinstance(value, (int, float)):
        return float(value)
    binding = row.get("windows") or []
    if binding:
        return min(item["remaining"] for item in binding)
    return -1.0


def _labels(found: dict[str, dict], *, high_only: bool) -> list[str]:
    picked = []
    for provider, label in (("claude", "opus"), ("grok", "grok"), ("codex", "sol")):
        row = found.get(provider) or {}
        if high_only:
            allowed = row.get("level") == "open"
        else:
            allowed = row.get("extra") == "open"
        if allowed:
            picked.append(label)
    return picked


def _route(rows: list[dict]) -> dict:
    found = _by_provider(rows)
    if not found:
        return dict(_CLOSED)
    code = "closed"
    if _open(found, "claude"):
        code = "sonnet"
    elif _open(found, "grok"):
        code = "grok"
    elif _open(found, "codex"):
        code = "sol"
    makers = [name for name in ("grok", "codex") if _open(found, name)]
    makers.sort(key=lambda name: _weekly(found, name), reverse=True)
    make = {"grok": "grok", "codex": "sol"}.get(makers[0], "closed") if makers else "closed"
    dispatch = _labels(found, high_only=False)
    dispatch_high = _labels(found, high_only=True)
    if "opus" in dispatch:
        judge = "opus"
    elif dispatch:
        # Opus is closed. The open maker with more weekly quota remaining takes command.
        judge = max(
            (("grok", _weekly(found, "grok")), ("sol", _weekly(found, "codex"))),
            key=lambda item: item[1],
        )[0]
        if judge not in dispatch:
            judge = dispatch[0]
    else:
        judge = "closed"
    if "opus" in dispatch_high:
        judge_high = "opus"
    elif "grok" in dispatch_high and (
        "sol" not in dispatch_high or _weekly(found, "grok") >= _weekly(found, "codex")
    ):
        judge_high = "grok"
    elif "sol" in dispatch_high:
        judge_high = "sol"
    else:
        judge_high = "closed"
    return {
        "code": code,
        "make": make,
        "judge": judge,
        "judge_high": judge_high,
        "dispatch": ",".join(dispatch) if dispatch else "closed",
        "dispatch_high": ",".join(dispatch_high) if dispatch_high else "closed",
    }


def _window_text(row: dict) -> str:
    if not row["windows"]:
        return "quota unreadable"
    parts = []
    for window in row["windows"]:
        reset = f" resets {window['resets_at']}" if window.get("resets_at") else ""
        parts.append(f"{window['label']} remaining {_pct(window['remaining'])}{reset}")
    return ", ".join(parts)


def render(decision: dict) -> str:
    rows = decision.get("rows") or []
    route = decision.get("route") or dict(_CLOSED)
    found = _by_provider(rows)
    lines = []
    order = (("claude", "Opus 5.5 and Sonnet 5.5"), ("codex", "GPT 6.1 Sol"), ("grok", "Grok 4.7"))
    if not rows:
        lines.append("WARNING quota unreadable — extra work CLOSED.")
    for provider, who in order:
        row = found.get(provider)
        name = _NAMES.get(provider, provider)
        if not row:
            lines.append(f"WARNING {name} quota missing — extra work CLOSED for {who}.")
            continue
        credit = ""
        if row.get("credits") == 0:
            credit = " Credits remaining 0."
        if row["extra"] == "closed":
            lines.append(
                f"WARNING {name} {_window_text(row)} — {row['level'].upper()}. "
                f"Extra work CLOSED for {who}.{credit}"
            )
        elif row["level"] == "warn":
            lines.append(
                f"WARNING {name} {_window_text(row)} — WARN. "
                f"Extra work still allowed for {who}. Stop at {STOP_REMAINING}% remaining.{credit}"
            )
        else:
            lines.append(f"{name} {_window_text(row)} — OPEN. Extra work allowed for {who}.{credit}")
    lines.append(
        f"RULE {DASHBOARD_RESERVE}% of each weekly window stays reserved for dashboard "
        f"daily and weekly runs. The other {100 - DASHBOARD_RESERVE}% is extra work. "
        f"Extra work stops at {STOP_REMAINING}% remaining. Warning at {WARN_REMAINING}%. "
        f"Review this split after {REVIEW_ON}."
    )
    lines.append(
        "ROUTE code={code} make={make} judge={judge} judge_high={judge_high} "
        "dispatch={dispatch} dispatch_high={dispatch_high}".format(**route)
    )
    lines.append(
        "DISPATCH Opus 5.5 commands by default. Grok 4.7 and GPT 6.1 Sol also command "
        "when the operator chooses that session, or when Opus is closed and that name is in dispatch. "
        "A high command turn is allowed only for a name in dispatch_high. Sonnet codes and does not command."
    )
    lines.append("Dashboard paper runs stay on their own clock. This command does not start them.")
    return "\n".join(lines) + "\n"


def _node_env() -> dict:
    """Put the newest nvm Node first on PATH. Under systemd the PATH finds /usr/bin/node (v18),
    which rejects paseo's `--disable-warning` flag, so every scheduled usage read failed and the
    run budget silently allowed every run (found 2026-11-03)."""
    env = dict(os.environ)
    versions = []
    for path in (Path.home() / ".nvm" / "versions" / "node").glob("v*/bin"):
        try:
            versions.append((tuple(int(x) for x in path.parent.name[1:].split(".")), path))
        except ValueError:
            continue
    if versions:
        env["PATH"] = str(max(versions)[1]) + os.pathsep + env.get("PATH", "")
    return env


def fetch_paseo() -> list:
    proc = subprocess.run(
        [_PASEO, "usage", "ls", "--json"],
        check=False, capture_output=True, text=True, timeout=60, env=_node_env(),
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError("paseo usage ls failed")
    payload = json.loads(proc.stdout)
    if not isinstance(payload, (list, dict)):
        raise RuntimeError("paseo usage ls returned an unexpected payload")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) >= 2 and args[0] == "--file":
            payload = json.loads(Path(args[1]).read_text())
        else:
            payload = fetch_paseo()
        sys.stdout.write(render(assess(payload)))
        return 0
    except Exception:
        sys.stdout.write(render({"rows": [], "route": dict(_CLOSED)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
