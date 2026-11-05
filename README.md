# r2d2-agent-workflow

Reusable multi-agent sessions, on-demand memory, token accounting, usage guards and project provenance.

Operate independent model sessions and inspect their work, memory and resource use without the trading engine.

## Features

- Independent provider sessions
- Explicitly requested team review
- Verbatim on-demand memory
- Token trends and unusual-use alerts
- Session provenance and secret redaction

## Use

python3 check.py; then python3 -m venv .venv and install requirements.txt; optional provider/Telegram authentication is separate.

Run `python3 check.py` for read-only source checks. These checks are not a claim
that external accounts, market feeds, hardware or a deployed service are configured.

## Snapshot and updates

Created on 2026-11-05 from the local R2D2 workspaces. This is a private source
snapshot; the original local workspaces remain the working copies. The catalogue
repo records every source and omission. Updates happen only on explicit operator
request; there is no automatic sync, push, hook or publishing timer.

Credentials, private chats/memory, live paper books, game saves, models, caches,
virtual environments and proprietary application binaries are not included.
Use a separate environment for this repo. Read USAGE.md/SETUP.md or the original
README where present. Existing integrations are documented rather than silently
starting account actions.
