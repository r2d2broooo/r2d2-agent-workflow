"""Provider-neutral @1 supervisor runner.

Claude Code and Codex keep their own native sessions, while this module owns the
small piece of shared state that decides which provider currently fills the @1
role.  A provider change injects the recent shared chat transcript so handoffs
do not start from a blank conversation.
"""

from __future__ import annotations

import os as _export_os
import pathlib as _export_pathlib

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import agent_registry, session_day
from .agents import run_claude

log = logging.getLogger("supervisor")

# Cloud supervisors are configured through the shared agent registry. The local
# model remains an independent agent, not a quota-failover provider.
PROVIDER_ROWS = agent_registry.get_supervisor_agents()
PROVIDERS = tuple(a["provider"] for a in PROVIDER_ROWS)
PROVIDER_LABELS = {a["provider"]: a.get("label", a["provider"]) for a in PROVIDER_ROWS}
RUNNER_PROVIDERS = {"claude", "codex"}
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / '.local/bin/codex')
CODEX_MODEL = os.environ.get("R2D2_CODEX_MODEL") or None
CODEX_TIMEOUT = int(os.environ.get("R2D2_CODEX_TIMEOUT", "900"))
CODEX_SANDBOX = os.environ.get("R2D2_CODEX_SANDBOX", "danger-full-access").strip().lower()
if CODEX_SANDBOX not in {"read-only", "workspace-write", "danger-full-access"}:
    log.warning("invalid R2D2_CODEX_SANDBOX=%r; using workspace-write", CODEX_SANDBOX)
    CODEX_SANDBOX = "workspace-write"

CONFIG_DIR = Path(os.environ.get("R2D2_CONFIG_DIR", Path(__file__).resolve().parents[2] / ".state" / "provider-config"))
CONFIG_FILE = CONFIG_DIR / "supervisor.json"
CONFIG_LOCK = CONFIG_DIR / "supervisor-config.lock"
SESSIONS_FILE = CONFIG_DIR / "codex-sessions.json"
SESSIONS_LOCK = CONFIG_DIR / "codex-sessions.lock"
TURN_LOCK_DIR = CONFIG_DIR / "supervisor-locks"
MODEL_STATUS_FILE = Path(str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / 'dashboard/data/model_status.json'))
MODEL_STATUS_LOCK = CONFIG_DIR / "model-status.lock"

DEFAULT_CONFIG = {
    "provider": "claude",
    "fallback_enabled": True,
    "last_switch_reason": "default",
    "updated_at": 0,
    "last_provider_by_workdir": {},
}

CLAUDE_MEMORY_INDEXES = (
    Path.home() / ".claude/projects/-home-user/memory/MEMORY.md",
    Path.home() / ".claude/projects/-home-user-Desktop/memory/MEMORY.md",
    Path.home() / ".claude/projects/-home-user-dashboard/memory/MEMORY.md",
    Path.home() / ".claude/projects/-home-user-dashboard-claude-workdir-group/memory/MEMORY.md",
)
MAX_MEMORY_CONTEXT_CHARS = int(os.environ.get("R2D2_SUPERVISOR_MEMORY_CHARS", "60000"))
MEMORY_MAX_AGE_SECONDS = int(os.environ.get("R2D2_MEMORY_MAX_AGE_DAYS", "30")) * 86400
CLAUDE_SYSTEM_PROMPT_FILE = Path(str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / 'telegram-bots/prompts/claude/system_prompt.md'))
CODEX_SYSTEM_PROMPT_FILE = Path(str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / 'telegram-bots/prompts/codex/system_prompt.md'))


def _codex_system_prompt_file(system_prompt_file: str | None) -> str | None:
    """Keep @3 independent even when a legacy caller supplies @1's prompt."""
    if not system_prompt_file:
        return None
    try:
        if Path(system_prompt_file).resolve() == CLAUDE_SYSTEM_PROMPT_FILE.resolve():
            return str(CODEX_SYSTEM_PROMPT_FILE)
    except OSError:
        pass
    return system_prompt_file


def _read_json(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else dict(default)
    except (OSError, ValueError, TypeError):
        return dict(default)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _update_json(path: Path, lock_path: Path, default: dict, update) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_json(path, default)
            result = update(data) or data
            _atomic_write_json(path, result)
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def get_supervisor_status() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_read_json(CONFIG_FILE, DEFAULT_CONFIG))
    if cfg.get("provider") not in PROVIDERS:
        cfg["provider"] = "claude"
    cfg["available"] = {
        provider: bool(next((a for a in agent_registry.status() if a.get("provider") == provider), {}).get("available"))
        for provider in PROVIDERS
    }
    cfg["labels"] = dict(PROVIDER_LABELS)
    cfg["agents"] = agent_registry.status()
    cfg["agent_count"] = len(cfg["agents"])
    cfg["supervisor_count"] = len(PROVIDER_ROWS)
    cfg["fallback_order"] = [p for p in PROVIDERS if p in RUNNER_PROVIDERS]
    cfg["failover_supported"] = len(cfg["fallback_order"]) > 1
    cfg["codex_sandbox"] = CODEX_SANDBOX
    return cfg


def set_supervisor(provider: str, fallback_enabled: bool | None = None,
                   reason: str = "manual") -> dict:
    provider = (provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"unknown supervisor {provider!r}; choose one of: {", ".join(PROVIDERS)}")

    def update(cfg: dict) -> dict:
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        merged["provider"] = provider
        if fallback_enabled is not None:
            merged["fallback_enabled"] = bool(fallback_enabled)
        merged["last_switch_reason"] = reason
        merged["updated_at"] = int(time.time())
        return merged

    _update_json(CONFIG_FILE, CONFIG_LOCK, DEFAULT_CONFIG, update)
    return get_supervisor_status()


def set_fallback_enabled(enabled: bool) -> dict:
    status = get_supervisor_status()
    return set_supervisor(status["provider"], fallback_enabled=enabled,
                          reason="automatic fallback enabled" if enabled else "automatic fallback disabled")


def _workdir_key(cwd: str) -> str:
    return os.path.realpath(os.path.expanduser(cwd))


def _last_provider(cwd: str) -> str | None:
    return get_supervisor_status().get("last_provider_by_workdir", {}).get(_workdir_key(cwd))


def _record_last_provider(cwd: str, provider: str) -> None:
    key = _workdir_key(cwd)

    def update(cfg: dict) -> dict:
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        by_dir = dict(merged.get("last_provider_by_workdir") or {})
        by_dir[key] = provider
        merged["last_provider_by_workdir"] = by_dir
        return merged

    _update_json(CONFIG_FILE, CONFIG_LOCK, DEFAULT_CONFIG, update)


def _get_codex_session(cwd: str) -> str | None:
    return _read_json(SESSIONS_FILE, {}).get(_workdir_key(cwd))


def _set_codex_session(cwd: str, session_id: str) -> None:
    key = _workdir_key(cwd)

    def update(data: dict) -> dict:
        data[key] = session_id
        return data

    _update_json(SESSIONS_FILE, SESSIONS_LOCK, {}, update)


def clear_codex_session(cwd: str) -> bool:
    key = _workdir_key(cwd)
    removed = False

    def update(data: dict) -> dict:
        nonlocal removed
        removed = data.pop(key, None) is not None
        return data

    _update_json(SESSIONS_FILE, SESSIONS_LOCK, {}, update)
    return removed


def _codex_prompt(prompt: str, system_prompt_file: str | None,
                  new_session: bool, cwd: str, isolated: bool = False) -> str:
    if isolated:
        # The cloud-trader contract is deliberately prompt-complete. Do not prepend the generic
        # supervisor identity, a system-prompt file, or Claude's memory indexes.
        return prompt
    if not new_session:
        return prompt
    identity = (
        "You are Codex by OpenAI, the independent R2D2 @3 supervisor. Claude is the separate "
        "@1 supervisor and the local model is @2. You have the same orchestration authority "
        "and capabilities as Claude, but you receive and execute your own tasks concurrently. "
        "Do not claim that you are Claude or wait for Claude unless the user explicitly asks "
        "the agents to coordinate. Preserve @2's independent prediction role.\n\n"
    )
    if system_prompt_file:
        try:
            instructions = Path(system_prompt_file).read_text().strip()
            memory, memory_sources = _memory_context_details(cwd, consumer="codex")
            inherited_block = (f"\n\nINHERITED PROJECT CONTEXT:\n{memory}" if memory else "")
            return (f"{identity}R2D2 cloud-supervisor operating instructions:\n\n"
                    f"{instructions}{inherited_block}\n\nUSER TASK:\n{prompt}")
        except OSError as exc:
            log.warning("could not read supervisor prompt %s: %s", system_prompt_file, exc)
    return identity + prompt


def _parse_turn_error(stdout: str) -> str:
    """The last failed-turn or stream error message from `codex exec --json` output, if any."""
    message = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.failed":
            message = str((event.get("error") or {}).get("message") or message)
        elif event.get("type") == "error" and event.get("message"):
            message = str(event["message"])
    return message


def _parse_thread_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def _parse_agent_message(stdout: str) -> str:
    answers: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text")
            if text:
                answers.append(str(text))
    return answers[-1].strip() if answers else ""


REASONING_EFFORTS = ("low", "medium", "high")
_LAST_CODEX_USAGE: dict[str, dict] = {}

CLOUD_TRADER_CODEX_DISABLED_FEATURES = (
    # No shell/code runner means the model cannot open either paper-book JSON directly.
    "shell_tool", "unified_exec", "code_mode", "code_mode_host",
    # No delegation, workspace apps/plugins, browser automation or computer control.
    "multi_agent", "apps", "plugins", "skill_search", "browser_use",
    "browser_use_external", "browser_use_full_cdp_access", "computer_use", "in_app_browser",
)


def _codex_add_dirs() -> list[str]:
    configured = os.environ.get("R2D2_CODEX_ADD_DIRS")
    candidates = (configured.split(os.pathsep) if configured else [
        str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / 'dashboard'),
        str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / 'telegram-bots'),
        str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / 'trading-wiki'),
    ])
    return [os.path.realpath(p) for p in candidates if p and os.path.isdir(p)]


async def _codex_once(prompt: str, cwd: str, session_id: str | None,
                      system_prompt_file: str | None, model: str | None,
                      isolated: bool = False, thinking: str | None = None,
                      search: bool = False) -> tuple[int, str, str, str | None]:
    from . import token_guard, token_usage
    paused = token_guard.check("codex", token_usage.classify(cwd, isolated))
    if paused:
        return 1, "", token_guard.refusal("codex", paused), session_id
    effort_args = ["--config", f'model_reasoning_effort="{thinking}"'] if thinking in REASONING_EFFORTS else []
    new_session = not session_id
    sandbox = "read-only" if isolated else CODEX_SANDBOX
    started = time.time()
    fd, output_path = tempfile.mkstemp(prefix="r2d2-codex-", suffix=".txt")
    os.close(fd)
    try:
        if session_id:
            args = [
                CODEX_BIN, "exec", "resume", "--json",
                "--config", f'sandbox_mode="{sandbox}"',
                "--config", 'approval_policy="never"',
            ]
            args += effort_args
            if model:
                args += ["--model", model]
            args += [session_id, "-"]
        else:
            args = [CODEX_BIN, "exec", "--json", "--color", "never",
                    "--sandbox", sandbox,
                    "--config", 'approval_policy="never"', "--cd", cwd,
                    "--output-last-message", output_path]
            if isolated:
                # One-shot, prompt-only trader. Internet search is off unless this call
                # demands it. Chart PNGs in its own cwd stay visible. No shell, browser,
                # plugin, app or repository tool.
                args += [
                    "--ephemeral", "--ignore-user-config", "--ignore-rules", "--strict-config",
                    "--config", "project_doc_max_bytes=0",
                ]
                if search:
                    args += ["--config", 'web_search="live"']
                for feature in CLOUD_TRADER_CODEX_DISABLED_FEATURES:
                    args += ["--disable", feature]
                # Isolated runs ignore the user config. gpt-6.1-sol otherwise falls
                # back to low. Pass the effort only when this paper path asks for one.
                args += effort_args
            else:
                # Chats name their effort; the user config (high) is for the terminal.
                args += effort_args
                for path in _codex_add_dirs():
                    args += ["--add-dir", path]
            if model:
                args += ["--model", model]
            args += ["-"]

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=_codex_prompt(
                    prompt, system_prompt_file, new_session, cwd, isolated=isolated).encode()),
                timeout=CODEX_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            token_usage.record("codex", cwd=cwd, isolated=isolated, ok=False, started=started,
                               usage=None, model=model, effort=thinking, prompt_chars=len(prompt),
                               error="timeout")
            return -1, "", "__timeout__", session_id

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")
        try:
            answer = Path(output_path).read_text().strip()
        except OSError:
            answer = ""
        if not answer:
            answer = _parse_agent_message(stdout_text)
        if proc.returncode != 0 and not stderr_text.strip():
            # codex-cli 0.165 reports a failed turn (usage limit, auth, model errors) only as JSON
            # events on stdout. Without this the caller saw a bare "exit code 1", so a usage limit
            # was never classified as one and a review fired dozens of doomed requests (2026-10-29).
            stderr_text = _parse_turn_error(stdout_text)
        thread_id = _parse_thread_id(stdout_text) or session_id
        usage = token_usage.codex_usage(stdout_text)
        token_usage.record("codex", cwd=cwd, isolated=isolated, ok=proc.returncode == 0, started=started,
                           usage=usage, model=model, effort=thinking, prompt_chars=len(prompt),
                           reply_chars=len(answer),
                           error="" if proc.returncode == 0 else stderr_text.strip()[:300],
                           meter=None if isolated else token_usage.codex_meter(thread_id))
        if usage and proc.returncode == 0 and not isolated:
            _LAST_CODEX_USAGE[cwd] = usage
        return proc.returncode, answer, stderr_text, thread_id
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


async def run_codex(prompt: str, cwd: str, system_prompt_file: str | None = None,
                    model: str | None = None, isolated: bool = False,
                    thinking: str | None = None, search: bool = False) -> str:
    """Run Codex, resuming normal work or using a prompt-only one-shot trading session."""
    os.makedirs(cwd, exist_ok=True)
    if not isolated and not session_day.same_day(cwd):
        clear_codex_session(cwd)
        session_id = None
    else:
        session_id = None if isolated else _get_codex_session(cwd)
    # Operator 2026-11-03: GPT 6.1 Sol at medium everywhere (paper, Lab, diagnoses, chats).
    from . import model_choices
    model = model_choices.model("codex")
    effort = thinking or model_choices.effort("codex")
    rc, out, err, returned_id = await _codex_once(
        prompt, cwd, session_id, system_prompt_file, model or CODEX_MODEL,
        isolated=isolated, thinking=effort, search=search)

    # A removed/stale native transcript should only lose Codex's private session,
    # never the shared R2D2 transcript. A pre-sampling compaction failure is also
    # tied to the resumed thread: restarting the service just resumes the same
    # unusable transcript. Bootstrap a fresh native session once in either case.
    if rc != 0 and session_id and any(term in err.lower() for term in (
        "session not found", "thread not found", "no session", "could not find",
        "failed to run pre-sampling compact",
    )):
        clear_codex_session(cwd)
        rc, out, err, returned_id = await _codex_once(
            prompt, cwd, None, system_prompt_file, model or CODEX_MODEL,
            isolated=isolated, thinking=effort, search=search)

    if returned_id and rc == 0 and not isolated:
        _set_codex_session(cwd, returned_id)
        session_day.mark(cwd)
        from .agents import _reset_if_oversized
        _reset_if_oversized(cwd, _LAST_CODEX_USAGE.pop(cwd, None))
    if err == "__timeout__":
        return "Codex timed out."
    if rc != 0:
        detail = err.strip() or out.strip() or f"exit code {rc}"
        return f"Codex encountered an error: {detail}"
    return out or "(no output)"


_CAPACITY_ERROR_TERMS = (
    ("credit balance", "credits"), ("credit limit", "credit limit"),
    ("usage limit", "usage limit"), ("session limit", "session limit"), ("quota", "quota"),
    # 2026-08-01: Claude returned "You've hit your weekly limit · resets 9am" and NONE of the
    # terms below matched, so a quota exhaustion was classified as a generic provider error —
    # the unit went failed and the healer retried something no restart can fix.
    ("weekly limit", "weekly limit"), ("hit your limit", "usage limit"),
    ("limit reached", "usage limit"), ("limit · resets", "usage limit"),
    ("out of credits", "credits"), ("insufficient credit", "credits"),
    ("rate limit", "rate limit"), ("rate_limit", "rate limit"),
    ("too many requests", "rate limit"), ("billing", "billing"),
    ("failed to authenticate", "authentication"),
    ("5-hour limit", "session limit"),
    # shared/token_guard.py refused the call before it reached the model.
    ("paused by token guard", "token guard"),
    ("oauth session expired", "authentication"),
    ("authentication", "authentication"), ("not logged in", "not logged in"),
    ("login required", "login required"), ("unauthorized", "unauthorized"),
    ("subscription", "subscription"), ("capacity", "capacity"),
    ("overloaded", "overloaded"), ("file not found", "provider executable missing"),
    ("no such file", "provider executable missing"), ("executable not found", "provider executable missing"),
)


def _is_error(reply: str, provider: str) -> bool:
    low = (reply or "").strip().lower()
    label = PROVIDER_LABELS.get(provider, provider).lower()
    return any(low.startswith(prefix) for prefix in (
        f"{label} encountered an error:",
        f"{label} code encountered an error:",
        f"{label} runner failed:",
        "supervisor configuration error:",
    )) or low in {"", "(no output)"} or "timed out" in low or "timeout" in low


def _capacity_reason(reply: str) -> str | None:
    low = (reply or "").lower()
    for term, reason in _CAPACITY_ERROR_TERMS:
        if term in low:
            return reason
    return None


def _is_capacity_error(reply: str) -> bool:
    return _capacity_reason(reply) is not None


def classify_provider_reply(provider: str, reply: str) -> dict:
    """Classify provider output so callers can fail closed and show quota warnings."""
    probe = reply
    if str(probe).startswith("⚠️") and "\n" in str(probe):
        probe = str(probe).split("\n", 1)[1]
    reason = _capacity_reason(probe)
    if reason:
        return {"ok": False, "kind": "credit", "reason": reason,
                "warning": f"⚠️ {PROVIDER_LABELS.get(provider, provider)} unavailable ({reason}). Analysis was not completed; no action should be taken."}
    if _is_error(probe, provider):
        return {"ok": False, "kind": "provider", "reason": "provider error",
                "warning": f"⚠️ {PROVIDER_LABELS.get(provider, provider)} analysis failed. No action should be taken until a valid answer is produced."}
    return {"ok": True, "kind": "ok", "reason": None, "warning": None}


def _record_provider_event(provider: str, reply: str) -> None:
    """Persist latest provider health so quota failures remain visible."""
    try:
        status = classify_provider_reply(provider, reply)

        def update(data: dict) -> dict:
            providers = data.setdefault("providers", {})
            event = {"ts": time.time(), "provider": provider, "ok": bool(status["ok"]),
                     "kind": status["kind"], "reason": status.get("reason")}
            providers[provider] = {**event, "warning": status.get("warning")}
            events = data.setdefault("events", [])
            events.append(event)
            data["events"] = events[-100:]
            data["updated_ts"] = event["ts"]
            return data

        MODEL_STATUS_LOCK.parent.mkdir(parents=True, exist_ok=True)
        _update_json(MODEL_STATUS_FILE, MODEL_STATUS_LOCK, {"providers": {}, "events": []}, update)
    except Exception:
        log.exception("could not persist provider status for %s", provider)


def provider_warning(provider: str, reply: str) -> str | None:
    """Return a visible warning marker for provider failures, or None when healthy."""
    return classify_provider_reply(provider, reply).get("warning")


def _shared_transcript(cwd: str, limit: int = 40) -> str:
    """Best-effort cross-provider handoff for the shared personal/group rooms."""
    try:
        from . import chat_log, group_chat

        key = _workdir_key(cwd)
        mapping = {
            _workdir_key(group_chat.PERSONAL_CLAUDE_WORKDIR): group_chat.CLAUDE_PERSONAL_CHAT_ID,
            _workdir_key(group_chat.GROUP_WORKDIR): group_chat.GROUP_CHAT_ID,
        }
        chat_id = mapping.get(key)
        if chat_id is None:
            return ""
        messages = chat_log.get_recent_messages(chat_id, limit=limit)
        names = {"dashboard": "User", "claude_bot": "Supervisor", "codex_bot": "Supervisor",
                 "supervisor_bot": "Supervisor", "llm_bot": "Local @2"}
        return "\n".join(
            f"{names.get(m.get('bot_name'), m.get('bot_name', 'Unknown'))}: {m.get('text', '')}"
            for m in messages
        )
    except Exception as exc:
        log.warning("could not build supervisor handoff transcript: %s", exc)
        return ""


def _format_memory_age(age_seconds: float) -> str:
    if age_seconds < 3600:
        return f"{max(1, int(age_seconds // 60))}m"
    if age_seconds < 86400:
        return f"{max(1, int(age_seconds // 3600))}h"
    return f"{max(1, int(age_seconds // 86400))}d"


def _memory_context_details(cwd: str, consumer: str = "codex") -> tuple[str, list[dict]]:
    """Build provenance-labelled memory context and metadata for a handoff."""
    parts: list[str] = []
    sources: list[dict] = []
    remaining = MAX_MEMORY_CONTEXT_CHARS
    now = time.time()
    for path in CLAUDE_MEMORY_INDEXES:
        if remaining <= 0:
            break
        try:
            content = path.read_text().strip()
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if not content:
            continue
        age_seconds = max(0.0, now - mtime)
        age = _format_memory_age(age_seconds)
        stale = age_seconds > MEMORY_MAX_AGE_SECONDS
        source = {
            "path": str(path), "source": "claude", "generated_by": "Claude",
            "consumer": consumer, "age": age, "age_seconds": round(age_seconds),
            "stale": stale,
        }
        sources.append(source)
        status = "STALE — verify against current code/data" if stale else "fresh enough to use as a lead"
        content = content[:remaining]
        parts.append(
            f"Memory index (inherited from Claude; age {age}; status: {status}): {path}\n"
            f"Linked files resolve relative to: {path.parent}\n{content}"
        )
        remaining -= len(content)

    context_path = Path(cwd) / "CONTEXT.md"
    if remaining > 0 and context_path.is_file():
        try:
            content = context_path.read_text().strip()
            mtime = context_path.stat().st_mtime
        except OSError:
            content = ""
            mtime = 0.0
        if content:
            age_seconds = max(0.0, now - mtime)
            age = _format_memory_age(age_seconds)
            stale = age_seconds > MEMORY_MAX_AGE_SECONDS
            sources.append({
                "path": str(context_path), "source": "shared-handoff",
                "generated_by": "shared session context", "consumer": consumer,
                "age": age, "age_seconds": round(age_seconds), "stale": stale,
            })
            status = "STALE — verify" if stale else "fresh enough to use as a lead"
            parts.append(
                f"Current shared session context (generated by shared handoff; age {age}; status: {status}):\n{content[:remaining]}"
            )

    if not parts:
        return "", sources
    header = (
        "MEMORY PROVENANCE POLICY\n"
        f"Consumer: {consumer}. Freshness threshold: {max(1, MEMORY_MAX_AGE_SECONDS // 86400)} days.\n"
        "Inherited memory is continuity context, not verified truth. Prefer newer explicit user decisions and current code/data.\n\n"
    )
    return header + "\n\n".join(parts), sources


def _claude_memory_context(cwd: str, consumer: str = "codex") -> str:
    """Compatibility wrapper returning provenance-labelled Claude memory."""
    return _memory_context_details(cwd, consumer=consumer)[0]


def memory_sources_for_new_codex_session(cwd: str, consumer: str = "codex") -> list[dict]:
    """Return sources Codex will inherit when its native session is bootstrapped."""
    if _get_codex_session(cwd):
        return []
    return _memory_context_details(cwd, consumer=consumer)[1]


def _with_handoff(prompt: str, cwd: str, previous: str | None, provider: str,
                  include_metadata: bool = False):
    needs_handoff = bool(previous and previous != provider)
    codex_needs_memory = provider == "codex" and (
        previous != "codex" or not _get_codex_session(cwd)
    )
    if codex_needs_memory:
        needs_handoff = True
    memory_sources: list[dict] = []
    if not needs_handoff:
        return (prompt, memory_sources) if include_metadata else prompt
    transcript = _shared_transcript(cwd)
    memory, memory_sources = _memory_context_details(cwd, consumer=provider) if codex_needs_memory else ("", [])
    if not transcript and not memory:
        return (prompt, memory_sources) if include_metadata else prompt
    prior = PROVIDER_LABELS.get(previous or "", "the previous supervisor")
    current = PROVIDER_LABELS.get(provider, provider)
    sections = []
    if memory:
        sections.append(memory)
    if transcript:
        sections.append(f"RECENT SHARED TRANSCRIPT\n{transcript}")
    inherited = "\n\n=== INHERITED CONTEXT ===\n" + "\n\n".join(sections) + "\n=== END INHERITED CONTEXT ==="
    enriched = (
        f"SUPERVISOR HANDOFF: @1 changed from {prior} to {current}. Continue seamlessly. "
        "Inherited memory is provenance-labelled continuity context; verify stale items against current code/data. "
        "Do not repeat the context or mention the handoff unless the user asks."
        f"{inherited}\n\n"
        f"CURRENT TASK:\n{prompt}"
    )
    return (enriched, memory_sources) if include_metadata else enriched


async def _run_provider(provider: str, prompt: str, cwd: str,
                        system_prompt_file: str | None, settings_file: str | None,
                        model: str | None, isolated: bool = False,
                        thinking: str | None = None, search: bool = False,
                        shared_context: str = "") -> str:
    """Dispatch only to the configured provider; never silently use Claude.

    `shared_context` opens the task. Claude receives it as appended system prompt so every
    batch of a cycle reads it from cache; the other providers get it at the prompt's start."""
    effort = thinking if thinking in ("medium", "high") else None
    if shared_context and provider != "claude":
        prompt, shared_context = shared_context + prompt, ""
    try:
        if provider == "grok":
            from .grok_runner import run_grok
            return await run_grok(prompt, cwd, system_prompt_file, model,
                                  isolated=isolated, thinking=effort, search=search)
        if provider == "codex":
            codex_model = model if model and not model.startswith("claude-") else None
            prompt_file = None if isolated else _codex_system_prompt_file(system_prompt_file)
            return await run_codex(
                prompt, cwd, prompt_file, codex_model, isolated=isolated,
                thinking=effort, search=search)
        if provider == "claude":
            claude_model = model if model and model.startswith("claude-") else None
            return await run_claude(
                prompt, cwd,
                None if isolated else system_prompt_file,
                None if isolated else settings_file,
                claude_model,
                isolated=isolated,
                thinking=effort,
                search=search,
                shared_context=shared_context,
            )
        return f"Supervisor configuration error: provider {provider!r} has no runner adapter."
    except Exception as exc:
        log.exception("supervisor runner failed provider=%s cwd=%s", provider, cwd)
        return f"{PROVIDER_LABELS.get(provider, provider)} runner failed: {exc}"


async def run_named_supervisor(provider: str, prompt: str, cwd: str,
                               system_prompt_file: str | None = None,
                               settings_file: str | None = None,
                               model: str | None = None,
                               isolated: bool = False,
                               thinking: str | None = None,
                               search: bool = False,
                               shared_context: str = "") -> str:
    """Run one independent supervisor without switching or falling back.

    Claude @1 and Codex @3 use different workdirs, native sessions, and lock files,
    so they can work concurrently and never consume each other's assigned task.
    """
    selected = provider.lower()
    if selected not in PROVIDERS:
        return f"Supervisor configuration error: unknown provider {selected!r}."
    os.makedirs(cwd, exist_ok=True)
    TURN_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_key = f"{selected}:{_workdir_key(cwd)}"
    lock_name = hashlib.sha256(lock_key.encode()).hexdigest() + ".lock"
    with (TURN_LOCK_DIR / lock_name).open("a+") as turn_lock:
        await asyncio.to_thread(fcntl.flock, turn_lock.fileno(), fcntl.LOCK_EX)
        try:
            reply = await _run_provider(
                selected, prompt, cwd, system_prompt_file, settings_file, model,
                isolated=isolated, thinking=thinking, search=search, shared_context=shared_context)
            _record_provider_event(selected, reply)
            warning = provider_warning(selected, reply)
            return f"{warning}\n{reply}" if warning else reply
        finally:
            await asyncio.to_thread(fcntl.flock, turn_lock.fileno(), fcntl.LOCK_UN)


async def run_supervisor_result(prompt: str, cwd: str, system_prompt_file: str | None = None,
                                settings_file: str | None = None, model: str | None = None,
                                provider: str | None = None) -> dict:
    """Run the selected supervisor and return reply plus failover metadata.

    Only pre-response capacity/auth/provider-startup failures are replayed. A
    timeout or arbitrary runner error is returned without replaying the task,
    because it may have already performed partial tool work.
    """
    status = get_supervisor_status()
    selected = (provider or status["provider"]).lower()
    result = {
        "reply": "", "provider": selected, "requested_provider": selected,
        "failed_over": False, "failover_reason": None, "attempted": [selected],
        "memory_sources": [],
    }
    if selected not in PROVIDERS:
        result["reply"] = f"Supervisor configuration error: unknown provider {selected!r}."
        result["failover_reason"] = "unknown provider"
        return result

    os.makedirs(cwd, exist_ok=True)
    TURN_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(_workdir_key(cwd).encode()).hexdigest() + ".lock"
    with (TURN_LOCK_DIR / lock_name).open("a+") as turn_lock:
        await asyncio.to_thread(fcntl.flock, turn_lock.fileno(), fcntl.LOCK_EX)
        try:
            previous = _last_provider(cwd)
            enriched, memory_sources = _with_handoff(prompt, cwd, previous, selected, include_metadata=True)
            result["memory_sources"] = memory_sources
            reply = await _run_provider(selected, enriched, cwd, system_prompt_file, settings_file, model)
            result["reply"] = reply
            reason = _capacity_reason(reply) if _is_error(reply, selected) else None
            if not (reason and status.get("fallback_enabled", True)):
                if reason and not status.get("fallback_enabled", True):
                    result["failover_reason"] = f"{reason}; failover disabled"
                elif _is_error(reply, selected):
                    result["failover_reason"] = "timeout or non-replayable runner failure"
                else:
                    _record_last_provider(cwd, selected)
                return result

            fallback = next((p for p in PROVIDERS if p != selected and p in RUNNER_PROVIDERS), None)
            if not fallback:
                result["failover_reason"] = f"{reason}; no alternate supervisor configured"
                return result
            result["attempted"].append(fallback)
            log.warning("supervisor failover selected=%s fallback=%s reason=%s cwd=%s",
                        selected, fallback, reason, cwd)
            fallback_prompt, fallback_memory_sources = _with_handoff(prompt, cwd, selected, fallback, include_metadata=True)
            result["memory_sources"] = fallback_memory_sources or result.get("memory_sources", [])
            fallback_reply = await _run_provider(
                fallback, fallback_prompt, cwd, system_prompt_file, settings_file, model)
            if not _is_error(fallback_reply, fallback):
                switch_reason = f"automatic failover: {PROVIDER_LABELS[selected]} {reason}"
                set_supervisor(fallback, reason=switch_reason)
                _record_last_provider(cwd, fallback)
                result.update({
                    "reply": fallback_reply, "provider": fallback,
                    "failed_over": True, "failover_reason": switch_reason,
                })
                return result

            result["reply"] = f"{reply}\n\nFallback {PROVIDER_LABELS[fallback]} also failed:\n{fallback_reply}"
            result["failover_reason"] = f"{reason}; fallback {fallback} failed"
            return result
        finally:
            await asyncio.to_thread(fcntl.flock, turn_lock.fileno(), fcntl.LOCK_UN)


async def run_supervisor(prompt: str, cwd: str, system_prompt_file: str | None = None,
                         settings_file: str | None = None, model: str | None = None,
                         provider: str | None = None) -> str:
    """Compatibility wrapper returning only the supervisor reply."""
    result = await run_supervisor_result(
        prompt, cwd, system_prompt_file, settings_file, model, provider)
    return result["reply"]
