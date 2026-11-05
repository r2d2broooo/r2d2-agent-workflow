"""Session reports archived verbatim for on-demand retrieval.

Each short-term report is also a long-term input, with no model call,
filtering, truncation or weekly summary. Fresh prompts do not load this store.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
ROOT = Path(os.environ.get(
    "R2D2_SESSION_MEMORY",
    str(Path(__file__).resolve().parents[1] / "data" / "memory"),
))
_CODE = re.compile(r"^(STM|LTM)-[0-9]{8}")


def _now(when: datetime | None = None) -> datetime:
    current = when or datetime.now(LONDON)
    if current.tzinfo is None:
        current = current.replace(tzinfo=LONDON)
    return current.astimezone(LONDON)


def _stamp(when: datetime) -> tuple[str, str]:
    return when.date().isoformat(), when.strftime("%H:%M:%S")


def _write(path: Path, record: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def save_short(*, name: str, model: str, note: str, kind: str = "session",
               when: datetime | None = None) -> dict:
    """Save one daily session. The code is the handle a later task can pull."""
    current = _now(when)
    date, clock = _stamp(current)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{model}-{kind}".lower()).strip("-")[:24]
    code = f"STM-{current.strftime('%Y%m%d-%H%M%S%f')}-{slug or 'session'}"
    record = {
        "layer": "short",
        "name": " ".join(str(name or kind).split())[:120],
        "code": code,
        "date": date,
        "time": clock,
        "model": str(model or "unknown")[:80],
        "kind": str(kind or "session")[:40],
        "note": str(note or ""),
    }
    _write(ROOT / "short" / f"{code}.json", record)
    archive_input(record)
    return record


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("code") else None


def find(query: str = "", *, layer: str = "short", limit: int = 20,
         kind: str = "") -> list[dict]:
    """Index rows only. The note is a short preview, not the whole history."""
    folder = ROOT / ("long" if layer == "long" else "short")
    if not folder.is_dir():
        return []
    needle = " ".join(str(query or "").lower().split())
    rows = []
    for path in sorted(folder.rglob("*.json"), reverse=True):
        record = _read(path)
        if not record or (kind and record.get("kind") != kind):
            continue
        hay =" ".join(str(record.get(key) or "") for key in
                       ("name", "code", "date", "model", "note")).lower()
        if needle and needle not in hay:
            continue
        rows.append({
            "name": record.get("name"),
            "code": record.get("code"),
            "date": record.get("date"),
            "time": record.get("time"),
            "model": record.get("model"),
            "kind": record.get("kind"),
            "models": record.get("models"),
            "date_range": record.get("date_range"),
            "preview": str(record.get("note") or "")[:240],
        })
        if len(rows) >= max(1, limit):
            break
    return rows


def pull(code: str) -> dict | None:
    """One record. Refuse a code that is not a memory handle."""
    handle = str(code or "").strip()
    if not _CODE.match(handle) or "/" in handle or ".." in handle:
        return None
    layer = "long" if handle.startswith("LTM-") else "short"
    path = ROOT / layer / f"{handle}.json"
    if not path.is_file():
        matches = list((ROOT / layer).rglob(f"{handle}*.json")) if (ROOT / layer).is_dir() else []
        path = matches[0] if len(matches) == 1 else None
    return _read(path) if path else None


def archive_input(record: dict) -> dict:
    """Preserve one session report as a long-term input without summarising it."""
    code = "LTM-" + record["code"].removeprefix("STM-")
    archived = dict(record, layer="long", code=code, source_code=record["code"])
    return _write(ROOT / "long" / "inputs" / f"{code}.json", archived)


def archive_inputs() -> int:
    """Backfill existing short-term reports; leave existing inputs untouched."""
    count = 0
    for path in sorted((ROOT / "short").glob("STM-*.json")):
        record = _read(path)
        if not record:
            continue
        target = ROOT / "long" / "inputs" / ("LTM-" + record["code"][4:] + ".json")
        if not target.exists():
            archive_input(record)
            count += 1
    return count
