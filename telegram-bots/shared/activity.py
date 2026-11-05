"""System activity log — records everything we DO (not just chats): scans, analyses,
backtests, strategy changes, social posts, reports. So the dashboard shows a connected
timeline of what was developed/shared, and daily memory captures no-chat days too.

Lightweight append-only JSONL. One source of truth for the dashboard Activity feed +
the end-of-day report.
"""
import json
import os
import time

LOG = os.path.join(os.path.dirname(__file__), "..", "data", "activity.jsonl")
os.makedirs(os.path.dirname(LOG), exist_ok=True)


def log(kind: str, summary: str, detail: str = "") -> None:
    """kind: scan|analysis|backtest|strategy|social|report|forecast|other."""
    try:
        rec = {"ts": time.time(), "kind": kind, "summary": summary[:300], "detail": detail[:600]}
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # never let logging break the action


def recent(limit: int = 100, since_ts: float | None = None) -> list:
    if not os.path.exists(LOG):
        return []
    out = []
    try:
        with open(LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if since_ts and r.get("ts", 0) < since_ts:
                    continue
                out.append(r)
    except Exception:
        return []
    return list(reversed(out))[:limit]


def day_summary(day_start_ts: float) -> str:
    """A compact text digest of a day's activity, for the end-of-day memory report."""
    items = [r for r in recent(1000) if r.get("ts", 0) >= day_start_ts]
    if not items:
        return ""
    from collections import Counter
    by = Counter(r["kind"] for r in items)
    lines = [f"Activity today: {len(items)} actions (" + ", ".join(f"{k}×{n}" for k, n in by.items()) + ")"]
    for r in items[:25]:
        lines.append(f"- [{r['kind']}] {r['summary']}")
    return "\n".join(lines)
