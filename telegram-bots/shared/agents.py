
import os as _export_os
import pathlib as _export_pathlib
import asyncio
import json
import logging
import math
import os
import tempfile
import time

import requests

log = logging.getLogger(__name__)

CLAUDE_BIN = str(_export_pathlib.Path(_export_os.environ.get('R2D2_WORKSPACE', str(_export_pathlib.Path(__file__).resolve().parents[2]))) / '.local/bin/claude')
CLAUDE_TIMEOUT = int(os.environ.get("R2D2_CLAUDE_TIMEOUT", "900"))  # matches CODEX_TIMEOUT
OLLAMA_TIMEOUT = 300
# Keep the local model loaded between messages: qwen3 8B takes ~10s to reload after Ollama's default
# 5-minute idle unload. It holds ~6 GB RAM/VRAM while idle; nothing else uses the GPU (2026-10-04).
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

# ---- Local-model context-budget guard (@2) -------------------------------------------------
# num_ctx is otherwise unset, so Ollama silently truncates an over-long prompt to the model's
# default window — dropping from the FRONT (the system prompt), which degrades/hallucinates the
# reply. We instead (1) set num_ctx explicitly so the window is known, and (2) fit the prompt
# under a token budget OURSELVES — keeping the system prompt + most recent turns and truncating
# any single oversized message — so what gets dropped is chosen, not blindly cut by the server.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "4096"))       # KV-cache safe for a 4B model on 6GB
OLLAMA_OUTPUT_RESERVE = int(os.environ.get("OLLAMA_OUTPUT_RESERVE", "1024"))  # room left for the reply
_CHARS_PER_TOKEN = 3.5   # conservative (over-estimates tokens → trims a little early, never overflows)


def _est_tokens(text: str) -> int:
    return math.ceil(len(text or "") / _CHARS_PER_TOKEN)


def _truncate_middle(text: str, max_tokens: int) -> str:
    """Keep the head and tail of an oversized message, elide the middle (where a paste/tool dump
    usually is) so both the ask and the latest data survive."""
    max_chars = max(200, int(max_tokens * _CHARS_PER_TOKEN))
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    return f"{text[:head]}\n…[{len(text) - max_chars} chars trimmed to fit context]…\n{text[-tail:]}"


def fit_messages(messages: list[dict], num_ctx: int = OLLAMA_NUM_CTX,
                 reserve: int = OLLAMA_OUTPUT_RESERVE) -> list[dict]:
    """Return `messages` shrunk to fit a token budget: all leading system messages are kept, then
    as many of the MOST RECENT non-system messages as fit; any single message over 60% of budget is
    middle-truncated first. Deterministic, dependency-free (char/token heuristic)."""
    budget = max(512, num_ctx - reserve)
    per_msg_cap = int(budget * 0.6)
    msgs = [dict(m) for m in messages]
    for m in msgs:                                   # 1) cap any single oversized message
        if _est_tokens(m.get("content", "")) > per_msg_cap:
            m["content"] = _truncate_middle(m.get("content", ""), per_msg_cap)
    sys_msgs = [m for m in msgs if m.get("role") == "system"]
    rest = [m for m in msgs if m.get("role") != "system"]
    used = sum(_est_tokens(m.get("content", "")) for m in sys_msgs)
    kept_rev = []
    for m in reversed(rest):                         # 2) keep newest-first until the budget is spent
        t = _est_tokens(m.get("content", ""))
        if used + t > budget:
            break
        used += t
        kept_rev.append(m)
    kept = list(reversed(kept_rev))
    if rest and not kept:                            # a single huge latest turn: keep it, truncated
        last = dict(rest[-1])
        last["content"] = _truncate_middle(last.get("content", ""), max(256, budget - used))
        kept = [last]
    fitted = sys_msgs + kept
    if len(fitted) < len(messages):
        log.info("context guard: trimmed %d→%d messages (~%d tok budget)",
                 len(messages), len(fitted), budget)
    return fitted


def _with_num_ctx(options: dict | None) -> dict:
    opts = dict(options or {})
    opts.setdefault("num_ctx", OLLAMA_NUM_CTX)
    return opts


# ---- Text-only model guard -------------------------------------------------------------------
# Ollama rejects ANY request carrying images with HTTP 400 when the model has no "vision"
# capability (the local model has been text-only since 2026-10-04 — now Qwen3 8B). Chart/screenshot
# paths still attach images, so for a text-only model we drop them silently (a "withheld" note
# was echoed back verbatim); the measured text data in the same message is what it reads.
_VISION_CACHE: dict[tuple[str, str], bool] = {}


def _model_has_vision(model: str, ollama_url: str) -> bool:
    key = (ollama_url, model)
    if key not in _VISION_CACHE:
        try:
            resp = requests.post(f"{ollama_url}/api/show", json={"model": model}, timeout=10)
            resp.raise_for_status()
            _VISION_CACHE[key] = "vision" in (resp.json().get("capabilities") or [])
        except Exception as e:  # noqa: BLE001 - unknown: send as-is, don't cache the failure
            log.warning("capability check failed for %s: %s", model, e)
            return True
    return _VISION_CACHE[key]


def _drop_images_if_text_only(messages: list[dict], model: str, ollama_url: str) -> list[dict]:
    if not any(m.get("images") for m in messages) or _model_has_vision(model, ollama_url):
        return messages
    log.info("text-only model %s: dropping %d image(s)", model,
             sum(len(m.get("images") or []) for m in messages))
    return [{k: v for k, v in m.items() if k != "images"} for m in messages]


# Isolated paper sessions read the supplied dashboard evidence and the chart files in
# their workdir. Internet search is off unless the task demands it (operator 2026-11-02).
# --restricted confines Read/Glob/Grep to that workdir. Safe mode and strict MCP
# isolation keep settings, plugins and connectors from reopening the repo.
CLOUD_TRADER_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]
CLOUD_TRADER_SEARCH_TOOLS = ["WebSearch", "WebFetch"]
CLOUD_TRADER_DISALLOWED_TOOLS = [
    # Claude Code 2.1.302 rejects unknown deny-rule names. Chrome is disabled by --no-chrome and
    # computer control is absent from the explicit --tools allowlist, so neither belongs here.
    "Bash", "Edit", "Write", "NotebookEdit", "Task",
]


def _claude_result(stdout: str) -> tuple[str, dict | None, bool]:
    """(reply text, usage, is_error) from `--output-format json`; raw text if it is not JSON."""
    from . import token_usage
    try:
        payload = json.loads(stdout)
    except ValueError:
        return stdout, None, False
    if not isinstance(payload, dict) or "result" not in payload and "is_error" not in payload:
        return stdout, None, False
    return str(payload.get("result") or ""), token_usage.claude_usage(payload), bool(payload.get("is_error"))


async def _claude_once(prompt: str, cwd: str, continue_session: bool,
                       system_prompt_file: str | None, settings_file: str | None,
                       model: str | None, extra_args: list[str] | None = None,
                       effort: str | None = None) -> tuple[int, str, str]:
    from . import token_guard, token_usage
    isolated = "--no-session-persistence" in (extra_args or [])
    paused = token_guard.check("claude", token_usage.classify(cwd, isolated))
    if paused:
        return 1, "", token_guard.refusal("claude", paused)
    args = [CLAUDE_BIN]
    if continue_session:
        args += ["-c"]
    # JSON output carries the real token counts (operator 2026-11-03); the reply is its "result".
    args += ["-p", prompt, "--output-format", "json"]
    if system_prompt_file:
        args += ["--append-system-prompt-file", system_prompt_file]
    if settings_file:
        args += ["--settings", settings_file]
    if model:
        args += ["--model", model]
    if extra_args:
        args += list(extra_args)

    started = time.time()
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        token_usage.record("claude", cwd=cwd, isolated=isolated, ok=False, started=started, usage=None,
                           model=model, effort=effort, prompt_chars=len(prompt), error="timeout")
        return -1, "", "__timeout__"
    text, usage, is_error = _claude_result(stdout.decode(errors="replace"))
    err = stderr.decode(errors="replace")
    rc = proc.returncode
    if is_error and rc == 0:
        rc = 1
    token_usage.record("claude", cwd=cwd, isolated=isolated, ok=rc == 0, started=started, usage=usage,
                       model=model, effort=effort, prompt_chars=len(prompt), reply_chars=len(text),
                       error="" if rc == 0 else (err.strip() or text.strip())[:300])
    if usage and rc == 0 and not isolated:
        _LAST_USAGE[cwd] = usage
    return rc, text, err


_LAST_USAGE: dict[str, dict] = {}


# A chat session whose conversation has grown past this many tokens starts fresh on the next
# message (the chat log keeps the history). Daily reset alone let one day's session reach ~230k.
CONTEXT_RESET_TOKENS = int(os.environ.get("R2D2_CONTEXT_RESET_TOKENS", "150000"))


def _reset_if_oversized(cwd: str, usage: dict | None) -> None:
    """Call after session_day.mark(cwd): the next message in this folder starts a new session."""
    from . import session_day, token_usage
    if usage and token_usage.context_size(usage) > CONTEXT_RESET_TOKENS:
        session_day.reset(cwd)
        log.info("session in %s passed %d tokens; the next message starts fresh", cwd, CONTEXT_RESET_TOKENS)


async def run_claude(prompt: str, cwd: str, system_prompt_file: str | None = None,
                      settings_file: str | None = None, model: str | None = None,
                      isolated: bool = False, thinking: str | None = None,
                      search: bool = False, shared_context: str = "") -> str:
    # Operator 2026-11-03: Opus 5.5 at medium everywhere; a requested Haiku/Sonnet gets Opus.
    from . import model_choices
    model = model_choices.model("claude")
    thinking = thinking or model_choices.effort("claude")
    if isolated:
        # Trading decisions must not inherit an earlier native conversation. The caller also
        # strips system/settings files, so this invocation receives only its task prompt.
        tools = list(CLOUD_TRADER_SEARCH_TOOLS) + list(CLOUD_TRADER_ALLOWED_TOOLS) if search else list(CLOUD_TRADER_ALLOWED_TOOLS)
        extra_args = [
            "--restricted", "--safe-mode", "--strict-mcp-config", "--no-chrome",
            "--no-session-persistence", "--disable-slash-commands",
            "--permission-prompts", "none",
            "--tools", *tools,
            "--allowedTools", *tools,
            "--disallowedTools", *CLOUD_TRADER_DISALLOWED_TOOLS,
        ]
        # Opus 5.5 medium is the default everywhere. High is only an explicit override.
        extra_args += ["--effort", "high" if thinking == "high" else "medium"]
        if shared_context:
            # The cycle-wide part (rules, policy, market) goes in the system prompt, which Claude
            # caches, so each later batch reads it from cache instead of writing it again.
            with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="r2d2-shared-", delete=False) as fh:
                fh.write(shared_context)
                shared_file = fh.name
            try:
                rc, out, err = await _claude_once(
                    prompt, cwd, False, shared_file, settings_file, model, extra_args=extra_args,
                    effort=thinking)
            finally:
                os.unlink(shared_file)
        else:
            rc, out, err = await _claude_once(
                prompt, cwd, False, system_prompt_file, settings_file, model,
                extra_args=extra_args, effort=thinking)
        if err == "__timeout__":
            return "Claude Code timed out."
        if rc != 0:
            detail = err.strip() or out.strip() or f"exit code {rc}"
            return f"Claude Code encountered an error: {detail}"
        return out.strip() or "(no output)"

    # Dashboard and analysis resume only inside the current London day. A new
    # morning starts a fresh native session. The chat log keeps older turns and
    # is read only when a task asks for them. Paper trading uses the isolated path.
    from . import session_day
    resume = session_day.same_day(cwd)
    effort_args = ["--effort", "high" if thinking == "high" else "medium"]
    rc, out, err = await _claude_once(
        prompt, cwd, resume, system_prompt_file, settings_file, model, effort_args,
        effort=thinking)
    detail = err.strip() or out.strip()
    if rc != 0 and resume and "no conversation found" in detail.lower():
        rc, out, err = await _claude_once(
            prompt, cwd, False, system_prompt_file, settings_file, model, effort_args,
            effort=thinking)

    if err == "__timeout__":
        return "Claude Code timed out."
    if rc != 0:
        detail = err.strip() or out.strip() or f"exit code {rc}"
        return f"Claude Code encountered an error: {detail}"
    session_day.mark(cwd)
    _reset_if_oversized(cwd, _LAST_USAGE.pop(cwd, None))
    return out.strip() or "(no output)"


# Sampling presets. Ollama's defaults (temp 0.8, random seed) make the model
# sample a different answer every call — fine for chat, but it flips analysis
# verdicts on the same ticker. ANALYSIS pins low temperature + a fixed seed so
# the same input yields the same verdict, without touching the model's reasoning
# (thinking stays ON). See predictions track record for why consistency matters.
# temperature 0.0 = greedy/deterministic: the SAME chart+data must give the SAME verdict on a
# re-run (no Strong-Buy↔Buy flips from sampling). Residual run-to-run drift is then only the live
# data moving (new candle / price), not the model. seed pinned belt-and-suspenders.
# num_predict caps a runaway greedy loop (a small model once generated for >300s on one analysis).
ANALYSIS_OPTIONS = {"temperature": 0.0, "top_p": 1.0, "top_k": 1, "seed": 42, "num_predict": 800}


def _query_ollama_sync(messages: list[dict], model: str, ollama_url: str,
                       options: dict | None = None) -> str:
    # Thinking is OFF (think:false). On the 4B Qwen3.5 vision model, leaving it ON
    # makes it reason endlessly, burn the token budget, never reach the templated
    # answer, and leak raw chain-of-thought into the reply. For our STRUCTURED tasks
    # (rating template, brief chat) we want the answer directly. Still fall back to
    # `thinking` if `content` ever comes back empty.
    messages = fit_messages(_drop_images_if_text_only(messages, model, ollama_url))
    payload = {"model": model, "messages": messages, "stream": False, "think": False,
               "keep_alive": OLLAMA_KEEP_ALIVE, "options": _with_num_ctx(options)}
    resp = requests.post(
        f"{ollama_url}/api/chat",
        json=payload,
        timeout=OLLAMA_TIMEOUT,
    )
    resp.raise_for_status()
    m = resp.json()["message"]
    return m.get("content") or m.get("thinking") or ""


async def query_ollama(messages: list[dict], model: str, ollama_url: str,
                       options: dict | None = None) -> str:
    return await asyncio.to_thread(_query_ollama_sync, messages, model, ollama_url, options)


def _query_ollama_message_sync(messages: list[dict], model: str, ollama_url: str, tools: list[dict]) -> dict:
    # Thinking OFF (think:false) — go straight to the tool call / answer (see note in
    # _query_ollama_sync). Guarantee a non-empty content as a fallback for callers.
    messages = fit_messages(_drop_images_if_text_only(messages, model, ollama_url))
    resp = requests.post(
        f"{ollama_url}/api/chat",
        json={"model": model, "messages": messages, "tools": tools, "stream": False,
              "think": False, "keep_alive": OLLAMA_KEEP_ALIVE, "options": _with_num_ctx(None)},
        timeout=OLLAMA_TIMEOUT,
    )
    resp.raise_for_status()
    m = resp.json()["message"]
    if not m.get("content") and not m.get("tool_calls") and m.get("thinking"):
        m["content"] = m["thinking"]
    return m


async def query_ollama_with_tools(messages: list[dict], model: str, ollama_url: str, tools: list[dict]) -> dict:
    """Like query_ollama but returns the full message dict (may include tool_calls)."""
    return await asyncio.to_thread(_query_ollama_message_sync, messages, model, ollama_url, tools)
