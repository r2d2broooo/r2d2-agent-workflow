"""One model and one effort per cloud provider, used everywhere (operator 2026-11-03).

Paper trading, Strategy Lab, health diagnoses, the self-healer, Telegram, dashboard chats,
report reviews and the terminals all use these. A caller that asks for another model of the
same provider gets this one. To change a choice, edit STANDARD and the terminal configs
(~/.claude/settings.json, ~/.codex/config.toml, ~/.grok/config.toml). The local model stays
separate (qwen3-8b-local, Ollama).

Helpers (diagnoses and self-healing) are not fixed to one provider: `pick_helper` takes the
cloud provider with the most weekly limit left whose short window still has room and that the
token guard is not holding.
"""
from __future__ import annotations

STANDARD = {
    "claude": {"model": "claude-opus-5-5", "effort": "medium", "label": "Opus 5.5"},
    "codex": {"model": "gpt-6.1-sol", "effort": "medium", "label": "GPT 6.1 Sol"},
    "grok": {"model": "grok-4.7", "effort": "medium", "label": "Grok 4.7"},
}
LOCAL_MODEL = "qwen3-8b-local"
HELPER_ORDER = ("codex", "claude", "grok")      # tie-break when two have the same room left
HELPER_SHORT_FLOOR = 15.0                       # % of the 5-hour/session window that must be left
HELPER_WEEKLY_FLOOR = 5.0                       # % of the week that must be left

# Every part of the system that runs an LLM. When a provider lists a new model, the operator is
# asked for the preferred model of each of these (shared/model_watch.py).
SECTIONS = (
    ("paper", "Paper trading (13:00 full review, 15:00 holdings check, weekend review)"),
    ("lab", "Strategy Lab rounds"),
    ("diagnose", "Health diagnoses (model-run-diagnose)"),
    ("healer", "Self-healer escalation"),
    ("telegram", "Telegram bots @1 Claude, @3 Codex, @4 Grok"),
    ("dashboard", "Dashboard chats (Claude, Codex, Grok, group, training, analysis)"),
    ("reports", "Report reviews"),
    ("terminal", "Your terminals (Claude Code, Codex CLI, Grok CLI)"),
    ("local", "Local model @2 (Telegram, local book research, Lab)"),
)


def model(provider: str) -> str:
    return STANDARD[provider]["model"]


def effort(provider: str) -> str:
    return STANDARD[provider]["effort"]


def provider_of(model_name: str | None) -> str | None:
    """Provider of a model id or a legacy picker key (haiku, sonnet, gpt-5.4, grok-4.6, ...)."""
    name = str(model_name or "").lower()
    if name.startswith(("claude", "opus", "sonnet", "haiku", "fable")):
        return "claude"
    if name.startswith(("gpt", "codex", "o3", "o4")):
        return "codex"
    if name.startswith("grok"):
        return "grok"
    return None


def pick_helper(source: str = "diagnose", accounts: dict | None = None) -> str | None:
    """Cloud provider for a diagnosis or a repair, or None when none has room.

    Most weekly limit left wins, among providers whose short window has at least
    HELPER_SHORT_FLOOR % left and that the token guard does not hold for this source. If the
    meters cannot be read, the first provider in HELPER_ORDER that is not held is used.
    """
    from shared import token_guard
    free = [p for p in HELPER_ORDER if not token_guard.check(p, source)]
    if accounts is None:
        try:
            from shared import run_budget
            accounts = run_budget.usage()
        except Exception:
            return free[0] if free else None
    best, best_left = None, -1.0
    for provider in free:
        account = accounts.get(provider)
        if not account:
            continue
        left = 100.0 - float(account.get("weekly_used") or 0)
        short = account.get("short_remaining")
        if left < HELPER_WEEKLY_FLOOR or (short is not None and float(short) < HELPER_SHORT_FLOOR):
            continue
        if left > best_left:
            best, best_left = provider, left
    if best is None and not any(accounts.get(p) for p in free):
        return free[0] if free else None          # no meter for any free provider: do not stall
    return best
