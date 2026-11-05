"""Configuration-driven registry for every R2D2 agent.

The registry is deliberately provider-neutral: Telegram aliases, dashboard chat
IDs, systemd units, and supervisor capabilities all come from one source.  Add a
future agent by placing an ``agents.json`` override at ``R2D2_AGENTS_FILE`` (or
``~/.config/r2d2/agents.json``); existing callers continue to use the stable
Claude/LLM/Codex ids and aliases.
"""
from __future__ import annotations

import os as _export_os
import pathlib as _export_pathlib

import json
import os
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

CONFIG_FILE = Path(os.environ.get("R2D2_AGENTS_FILE", Path.home() / ".config" / "r2d2" / "agents.json"))
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]+$")

DEFAULT_AGENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "claude", "alias": "1", "label": "Claude", "bot_name": "claude_bot",
        "service": "telegram-claude-bot", "chat_id": 0, "title": "Claude",
        "role": "supervisor", "provider": "claude", "enabled": True,
    },
    {
        # @2 = local stock analyst (operator 2026-10-04): Telegram messaging + data/chart pulls
        # from our dashboard system only. title stays "Local LLM" — it names the existing chat log.
        "id": "llm", "alias": "2", "label": "Local Analyst", "bot_name": "llm_bot",
        "service": "telegram-llm-bot", "chat_id": -1, "title": "Local LLM",
        "role": "stock-analyst", "provider": "local", "enabled": True,
    },
    {
        "id": "codex", "alias": "3", "label": "Codex", "bot_name": "codex_bot",
        "service": "telegram-codex-bot", "chat_id": -3, "title": "Codex",
        "role": "supervisor", "provider": "codex", "enabled": True,
    },
    {
        "id": "grok", "alias": "4", "label": "Grok", "chat_id": -4,
        "bot_name": "grok_bot", "service": "telegram-grok-bot",
        "title": "Grok", "role": "supervisor", "provider": "grok", "enabled": True,
    },
)


def _load_override() -> list[dict[str, Any]]:
    try:
        raw = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("agents", [])
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _merged_agents() -> list[dict[str, Any]]:
    merged = {item["id"]: deepcopy(item) for item in DEFAULT_AGENTS}
    order = [item["id"] for item in DEFAULT_AGENTS]
    for override in _load_override():
        agent_id = str(override.get("id", "")).strip().lower()
        if not agent_id or not _ALIAS_RE.match(agent_id):
            continue
        if agent_id not in merged:
            order.append(agent_id)
            merged[agent_id] = {"id": agent_id}
        merged[agent_id].update(override)
    result: list[dict[str, Any]] = []
    aliases: set[str] = set()
    for agent_id in order:
        item = merged[agent_id]
        if not item.get("enabled", True):
            continue
        alias = str(item.get("alias", "")).strip()
        if not alias or not _ALIAS_RE.match(alias) or alias in aliases:
            continue
        aliases.add(alias)
        item["id"] = agent_id
        item["alias"] = alias
        item["label"] = str(item.get("label") or agent_id)
        result.append(item)
    return result


def get_agents() -> list[dict[str, Any]]:
    """Return enabled agents in stable configured order."""
    return deepcopy(_merged_agents())


def get_agent(agent_id: str) -> dict[str, Any] | None:
    key = (agent_id or "").strip().lower()
    return next((a for a in get_agents() if a["id"] == key), None)


def get_agent_for_alias(alias: str) -> dict[str, Any] | None:
    key = (alias or "").strip().lstrip("@").lower()
    return next((a for a in get_agents() if a["alias"].lower() == key), None)


def get_supervisor_agents() -> list[dict[str, Any]]:
    return [a for a in get_agents() if a.get("role") == "supervisor" and a.get("provider")]


def service_names() -> list[str]:
    return [a["service"] for a in get_agents() if a.get("service")]


def aliases() -> list[str]:
    return [a["alias"] for a in get_agents()]


def _provider_available(provider: str) -> bool:
    if provider == "grok":
        return bool(shutil.which("grok") or (Path.home() / ".local/bin/grok").exists()) and (Path.home() / ".grok/auth.json").exists()
    if provider == "claude":
        return bool(shutil.which("claude") or Path(str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / '.local/bin/claude')).exists())
    if provider == "codex":
        return bool(shutil.which("codex") or Path(os.environ.get("CODEX_BIN", str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / '.local/bin/codex'))).exists())
    if provider == "local":
        return bool(os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    return bool(provider)


def status() -> list[dict[str, Any]]:
    """Return safe operator-facing metadata, including availability."""
    rows = []
    for agent in get_agents():
        row = deepcopy(agent)
        row["available"] = _provider_available(str(agent.get("provider", "")))
        rows.append(row)
    return rows
