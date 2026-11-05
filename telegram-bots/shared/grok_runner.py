"""Grok subscription CLI adapter; separate engineering and prompt-only trading sessions."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from . import token_guard, token_usage

BIN = os.environ.get("GROK_BIN") or str(Path.home() / ".local/bin/grok")
TIMEOUT = int(os.environ.get("R2D2_GROK_TIMEOUT", "900"))
SANDBOX = (sys.executable, "-I", str(Path(__file__).with_name("grok_sandbox.py")))
TRADER_DENY = ("Edit(**)", "Write(**)", "Bash", "Read(/home/**)", "Read(/proc/**)", "Read(/sys/**)", "Read(/etc/**)")
TRADER_CONFIG = '''
[compat.claude]
agents = false
hooks = false
mcps = false
rules = false
skills = false
sessions = false
[compat.cursor]
agents = false
hooks = false
mcps = false
rules = false
skills = false
sessions = false
[compat.codex]
hooks = false
skills = false
sessions = false
[memory]
enabled = false
[memory_v2]
enabled = false
[managed_mcps]
enabled = false
gateway_tools_enabled = false
[features]
subagent_bundle = false
[plugins]
disabled = ["frontend-design"]
[permission]
deny = ["Edit(**)", "Write(**)", "Bash", "Read(/home/**)", "Read(/proc/**)", "Read(/sys/**)", "Read(/etc/**)"]
'''
# Ids and the names the model is shown. Either spelling removes the tool.
TRADER_REMOVED_TOOLS = (
    "run_terminal_command", "run_terminal_cmd",
    "search_replace",
    "kill_command_or_subagent", "kill_task",
    "todo_write",
    "get_command_or_subagent_output", "get_task_output",
    "spawn_subagent", "task",
    "scheduler_create", "scheduler_delete", "scheduler_list",
    "monitor", "search_tool", "use_tool", "workflow",
    "enter_plan_mode", "exit_plan_mode",
    "ask_user_question", "send_feedback",
    "image_gen", "image_edit", "image_to_video", "reference_to_video",
    "write", "Agent",
)
WEB_TOOLS = ("web_search", "web_fetch")


def _removed_tools(search: bool) -> str:
    names = list(TRADER_REMOVED_TOOLS)
    if not search:
        names.extend(WEB_TOOLS)
    return ",".join(names)


# Tool calls of the latest trader session (kind + title), for audits of the agent loop.
LAST_TOOLS: list[dict] = []
_FILE_REFUSAL ="File access outside your working folder is disabled."
_SHARED_DIRS = ("charts", "data")


def _link_workspace(cwd: str, work: Path) -> None:
    """Give the session this book's charts and frozen data, and nothing else from the workdir."""
    for name in _SHARED_DIRS:
        source = Path(cwd) / name
        if not source.is_dir():
            continue
        for path in source.rglob("*"):
            target = work / path.relative_to(cwd)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(path, target)


def _client_response(msg: dict) -> dict:
    """Refuse an agent request. A permission reject lets the turn continue; an allow does not.

    A JSON-RPC error or outcome cancelled aborts the turn. File and terminal calls get a
    result that contains no file bytes.
    """
    method = msg.get("method")
    if method == "session/request_permission":
        options = (msg.get("params") or {}).get("options") or []
        reject = ""
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                kind = str(option.get("kind") or "")
                option_id = option.get("optionId")
                if kind.startswith("reject") and isinstance(option_id, str) and option_id:
                    reject = option_id
                    break
        outcome = {"outcome": "selected", "optionId": reject} if reject else {"outcome": "cancelled"}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": outcome}}
    if method == "fs/read_text_file":
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": _FILE_REFUSAL}}
    if isinstance(method, str) and (method.startswith("fs/") or method.startswith("terminal/")):
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    return {"jsonrpc": "2.0", "id": msg["id"],
            "error": {"code": -32601, "message": "Client capability disabled"}}


async def _trader(prompt: str, cwd: str, model: str | None, thinking: str | None = None,
                  search: bool = False, usage_out: dict | None = None) -> str:
    """Read-only session over a private copy of this book's workdir, like Claude and Codex."""
    usage_out = {} if usage_out is None else usage_out
    with tempfile.TemporaryDirectory(prefix="r2d2-grok-trader-") as temp:
        home = Path(temp) / "config"
        work = Path(temp) / "work"
        home.mkdir(mode=0o700)
        work.mkdir()
        _link_workspace(cwd, work)
        (home / "config.toml").write_text(TRADER_CONFIG)
        # Share only the native auth file, never skills, memory, plugins or engineering state.
        (home / "auth.json").symlink_to(Path.home() / ".grok/auth.json")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GROK_", "XAI_"))}
        env.update(GROK_HOME=str(home), GROK_DISABLE_AUTOUPDATER="1", TMPDIR=str(home))
        socket = str(home / "leader.sock")
        # A shared leader otherwise keeps the engineering configuration it loaded earlier.
        audit = await asyncio.create_subprocess_exec(
            BIN, "--cwd", str(work), "--leader-socket", socket, "inspect", "--json",
            cwd=str(work), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            audit_out, audit_err = await asyncio.wait_for(audit.communicate(), 60)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            audit.kill()
            await audit.wait()
            raise
        if audit.returncode:
            raise RuntimeError("Trading context audit failed: " + audit_err.decode(errors="replace")[-500:])
        discovered = json.loads(audit_out)
        if (discovered.get("projectInstructions") or discovered.get("hooks") or discovered.get("mcpServers")
                or any(not s.get("disabled", False) for s in discovered.get("skills", []))):
            raise RuntimeError("Trading isolation failed: unexpected instructions, skills, hooks or MCP tools")
        args = [str(Path(BIN).resolve()), "--cwd", str(work), "--leader-socket", socket,
                "--no-subagents", "--permission-mode", "dontAsk",
                "--disallowed-tools", _removed_tools(search)]
        if search:
            args += ["--tools", "web_search,web_fetch"]
        else:
            args += ["--disable-web-search"]
        # dontAsk runs read_file, list_dir and grep. grok_sandbox limits them to this folder.
        for rule in TRADER_DENY:
            args += ["--deny", rule]
        trader_rule = (
            "You are Grok, an independent paper trader. Follow the supplied task and evidence. "
            "You can read and search the charts and data in your working folder. You cannot run "
            "commands, access other books, or edit the system."
        )
        if not search:
            trader_rule += " Internet search is off. Use the dashboard evidence in the task."
        args += ["--system-prompt-override", trader_rule]
        if model:
            args += ["--model", model]
        if thinking:
            args += ["--reasoning-effort", thinking]
        args += ["agent", "--no-leader", "stdio"]
        launcher = (*SANDBOX, str(work), str(home)) if SANDBOX else ()
        proc = await asyncio.create_subprocess_exec(*launcher, *args, cwd=str(work), env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
        stderr_task = asyncio.create_task(proc.stderr.read())
        seq, answer, tools = 0, [], []
        LAST_TOOLS[:] = []

        async def request(method, params):
            nonlocal seq
            seq += 1
            rid = seq
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "method": method, "params": params}) + "\n").encode())
            await proc.stdin.drain()
            while line := await proc.stdout.readline():
                msg = json.loads(line)
                if msg.get("method") == "session/update":
                    update = msg.get("params", {}).get("update", {})
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        answer.append(update.get("content", {}).get("text", ""))
                    elif update.get("sessionUpdate") == "tool_call":
                        tools.append({"kind": update.get("kind") or "", "title": str(update.get("title") or "")[:160]})
                elif "method" in msg and "id" in msg:
                    # Never grant a filesystem, terminal or permission request from this client.
                    proc.stdin.write((json.dumps(_client_response(msg)) + "\n").encode())
                    await proc.stdin.drain()
                elif msg.get("id") == rid:
                    if msg.get("error"):
                        raise RuntimeError(msg["error"].get("message", "Grok ACP error"))
                    return msg.get("result", {})
            err = (await stderr_task).decode(errors="replace")
            raise RuntimeError(err.strip()[-2000:] or "Grok ACP closed unexpectedly")

        try:
            async with asyncio.timeout(TIMEOUT):
                init = await request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
                if "cached_token" not in {m["id"] for m in init.get("authMethods", [])}:
                    raise RuntimeError("Authentication required: run grok login")
                await request("authenticate", {"methodId": "cached_token", "_meta": {"headless": True}})
                session = await request("session/new", {"cwd": str(work), "mcpServers": []})
                result = await request("session/prompt", {"sessionId": session["sessionId"],
                                                          "prompt": [{"type": "text", "text": prompt.replace(f"{cwd.rstrip('/')}/", "")}]})
                usage_out.update(token_usage.grok_usage(result, acp=True) or {})
                # ACP reports no round count; each tool call is one step (shows the loop in the log).
                LAST_TOOLS[:] = tools
                if usage_out:
                    usage_out["steps"] = len(tools)
                    usage_out["turns"] = len(tools) + 1
                if result.get("stopReason") != "end_turn":
                    raise RuntimeError(f"Incomplete Grok response: {result.get('stopReason')}")
                return "".join(answer).strip() or "Grok encountered an error: empty response"
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            await stderr_task


async def run_grok(prompt: str, cwd: str, system_prompt_file=None, model=None, *,
                   isolated=False, thinking=None, search=False) -> str:
    paused = token_guard.check("grok", token_usage.classify(cwd, isolated))
    if paused:
        return "Grok encountered an error: " + token_guard.refusal("grok", paused)
    started, usage, reply = time.time(), {}, ""
    # Operator 2026-11-03: Grok 4.7 at medium everywhere, paper and chats alike.
    from . import model_choices
    model = model_choices.model("grok")
    thinking = thinking if thinking in ("medium", "high") else model_choices.effort("grok")
    try:
        if isolated:
            reply = await _trader(prompt, cwd, model, thinking=thinking,
                                  search=bool(search), usage_out=usage)
            return reply
        reply = await _headless(prompt, cwd, system_prompt_file, model, thinking, usage)
        return reply
    except asyncio.CancelledError:
        reply = "Grok encountered an error: cancelled"
        raise
    except asyncio.TimeoutError:
        reply = "Grok encountered an error: timed out"
        return reply
    except Exception as exc:
        reply = f"Grok encountered an error: {exc}"
        return reply
    finally:
        failed = reply.startswith("Grok encountered an error")
        token_usage.record("grok", cwd=cwd, isolated=isolated, ok=not failed, started=started,
                           usage=usage or None, model=model, effort=thinking, prompt_chars=len(prompt),
                           reply_chars=len(reply), error=reply[:300] if failed else "")


async def _headless(prompt, cwd, system_prompt_file, model, thinking, usage: dict) -> str:
    """Telegram, dashboard and helper chats: resumable within the London day, medium effort."""
    from . import session_day
    rules = Path(system_prompt_file).read_text() if system_prompt_file else ""
    with tempfile.TemporaryDirectory(prefix="r2d2-grok-prompt-") as temp:
        path = Path(temp) / "prompt.txt"
        path.write_text(prompt)
        args = [BIN, "--cwd", cwd, "--prompt-file", str(path), "--output-format", "json",
                "--permission-mode", "default", "--no-subagents",
                "--reasoning-effort", thinking if thinking in ("medium", "high") else "medium"]
        marker = Path(cwd) / ".r2d2-grok-session"
        if marker.exists() and not session_day.same_day(cwd):
            marker.unlink()
        if marker.exists():
            args += ["--resume", marker.read_text().strip()]
        if rules:
            args += ["--rules", rules]
        if model:
            args += ["--model", model]
        proc = await asyncio.create_subprocess_exec(*args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GROK_DISABLE_AUTOUPDATER": "1"})
        try:
            out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            with contextlib.suppress(ValueError, TypeError):
                usage.update(token_usage.grok_usage(json.loads(out)) or {})
            return "Grok encountered an error: " + (err or out).decode(errors="replace").strip()[-2000:]
        payload = json.loads(out)
        usage.update(token_usage.grok_usage(payload) or {})
        if payload.get("type") == "error" or payload.get("is_error"):
            return "Grok encountered an error: " + str(payload.get("message") or payload.get("result"))
        if payload.get("stopReason") not in (None, "end_turn"):
            return f"Grok encountered an error: incomplete response ({payload['stopReason']})"
        if payload.get("sessionId"):
            marker.write_text(payload["sessionId"])
            session_day.mark(cwd)
            from .agents import _reset_if_oversized
            _reset_if_oversized(cwd, usage)
        return str(payload.get("text") or payload.get("result") or "Grok encountered an error: empty response")
