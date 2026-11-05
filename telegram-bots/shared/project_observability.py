"""Read-only views over the existing token ledger, task list and Git repositories.

No model calls, new task store or additional usage collector. Paths are selected from
local repositories, never supplied to shell commands. Metrics are tokens, not invoices.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import token_usage
from .secret_redaction import redact_secrets

BASE = Path(__file__).resolve().parents[2]
LONDON = ZoneInfo('Europe/London')
TERMINAL = 'terminal'


def projects() -> dict[str, Path]:
    return {p.name: p.resolve() for p in [BASE, *sorted(BASE.iterdir())]
            if not p.name.startswith('.') and p.is_dir() and (p / '.git').exists()}


def project_path(name: str) -> Path:
    paths = projects()
    if name not in paths:
        raise ValueError('Choose a listed project repository')
    return paths[name]


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def snapshot(root: Path) -> dict:
    return {'repository': str(root), 'branch': _git(root, 'branch', '--show-current'),
            'head': _git(root, 'rev-parse', 'HEAD'),
            'dirty': bool(_git(root, 'status', '--porcelain', '--untracked-files=no')),
            'last_commit': redact_secrets(_git(root, 'log', '-1', '--format=%h %cs %s')),
            'working_files': _git(root, 'diff', '--name-only', 'HEAD').splitlines()[:100]}


def provenance(root: Path, before: dict, started: float) -> dict:
    after = snapshot(root)
    commits = []
    if before.get('head') and after.get('head') and before['head'] != after['head']:
        commits = _git(root, 'log', '--max-count=30', '--format=%H %s',
                       f"{before['head']}..{after['head']}").splitlines()
    return {'started': started, 'finished': time.time(), 'before': before, 'after': after,
            'observed_commits': [redact_secrets(c) for c in commits],
            'changed_files': _git(root, 'diff', '--name-only', before['head'], after['head']).splitlines()[:100]
                             if before.get('head') and after.get('head') else [],
            'attribution': 'Observed during this call; concurrent work may be included.'}


def instruction_revision() -> str:
    files = [BASE / 'r2d2-agents/AGENTS.md', BASE / 'telegram-bots/AGENTS.md', BASE / 'AGENTS.md']
    return hashlib.sha256(b'\0'.join(p.read_bytes() for p in files)).hexdigest()[:16]


def trends(rows: list[dict], roots: dict[str, Path], now: float | None = None) -> dict:
    now = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(now, LONDON).date()
    days = [(today - dt.timedelta(days=n)).isoformat() for n in range(8, -1, -1)]
    by_project = {}
    for row in rows:
        stamp = float(row.get('ts') or 0)
        if stamp > now:
            continue
        day = dt.datetime.fromtimestamp(stamp, LONDON).date().isoformat()
        if day not in days:
            continue
        path = Path(row.get('project_root') or row.get('cwd') or '/').resolve()
        matches = [(name, root) for name, root in roots.items() if path == root or root in path.parents]
        if matches:
            name = max(matches, key=lambda item: len(item[1].parts))[0]
        else:
            # Interactive terminals outside a repository (mostly the home folder).
            name = TERMINAL if str(row.get('source') or '').startswith('terminal:') else 'unassigned'
        rec = by_project.setdefault(name, {'project': name, 'days': {}, 'models': {}, 'unreported_calls': 0})
        bucket = rec['days'].setdefault(day, {'raw': 0, 'effective': 0, 'calls': 0, 'cached': 0})
        bucket['calls'] += token_usage.calls(row)
        raw = sum(int(row.get(k) or 0) for k in ('input', 'cached', 'cache_write', 'output'))
        bucket['raw'] += raw
        bucket['effective'] += int(row.get('effective') or 0)
        bucket['cached'] += int(row.get('cached') or 0)
        key = f"{row.get('provider') or '?'} / {row.get('model') or '?'} / {row.get('effort') or 'unreported effort'}"
        rec['models'][key] = rec['models'].get(key, 0) + raw
        if not row.get('reported', bool(raw)):
            rec['unreported_calls'] += token_usage.calls(row)
    output, alerts = [], []
    for name, rec in sorted(by_project.items()):
        series = [dict(date=d, **rec['days'].get(d, {'raw': 0, 'effective': 0, 'calls': 0, 'cached': 0})) for d in days]
        baseline = sum(d['effective'] for d in series[:7]) / 7
        active = sum(d['calls'] > 0 for d in series[:7])
        yesterday = series[-2]
        # Inactive/missing history isn't a credible baseline. Alert only; never pause jobs.
        unusual = active >= 3 and yesterday['effective'] >= 1_000_000 and yesterday['effective'] > baseline * 2
        out = {**rec, 'days': series, 'yesterday': yesterday, 'today': series[-1],
               'baseline_effective': round(baseline), 'baseline_active_days': active,
               'ratio': round(yesterday['effective'] / baseline, 2) if baseline else None,
               'unusual': unusual}
        output.append(out)
        if unusual:
            alerts.append({'project': name, 'date': yesterday['date'],
                           'message': f"{name}: yesterday's effective tokens were {out['ratio']}× the prior seven-day average."})
    return {'projects': output, 'alerts': alerts, 'timezone': 'Europe/London',
            'metric': 'Tokens only. Effective weights cache reads at 0.1; this is not billed cost.',
            'baseline_note': 'Previous seven complete days before yesterday; at least three active days required.'}


def usage_report() -> dict:
    roots = projects()
    # Read from the start of the oldest London day, including DST transitions.
    first = dt.datetime.now(LONDON).date() - dt.timedelta(days=8)
    since = dt.datetime.combine(first, dt.time(), LONDON).timestamp()
    return trends(token_usage.read(since), roots)


def terminal_card(usage: dict | None) -> dict:
    """Claude Code, Codex and Grok terminals outside a repository. Not selectable for sessions."""
    from . import session_memory
    latest = session_memory.find('', limit=1, kind='terminal')
    return {'name': TERMINAL, 'repository': '', 'branch': 'interactive terminals · not a repository',
            'head': '', 'dirty': False, 'working_files': [], 'selectable': False,
            'last_commit': 'Claude Code, Codex and Grok sessions outside a repository',
            'latest_session': latest[0]['name'] if latest else '', 'usage': usage}


def overview() -> dict:
    roots = projects()
    usage = usage_report()
    indexed = {r['project']: r for r in usage['projects']}
    cards = [{'name': name, **snapshot(root), 'usage': indexed.get(name)} for name, root in roots.items()]
    cards.append(terminal_card(indexed.get(TERMINAL)))
    return {'projects': cards, 'usage': usage, 'instruction_revision': instruction_revision()}
