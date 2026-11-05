# Grok @4 setup — 31 October 2026

Operator request: Grok gets its own independent paper trader and Strategy Lab
identity, and access to inspect and manage the existing R2D2 system.

## Access

- Official Grok Build CLI: `<workspace>/.local/bin/grok` (installed 1.0.70).
- Login uses the operator's Grok subscription, verified with `grok models`:
  `grok-4.7` default. `grok login --device-auth` reconnects the account.
- Start a terminal with `cd <workspace>/r2d2-agents && grok`.
- Dashboard: **Chat → Grok @4** for the main conversation, or Chat → R2D2 Agents
  to create a named **Grok** project session; or Terminal → **Grok @4**.
  Team rooms include Claude, Codex and Grok with separate contributions.
- Headless project sessions can use permitted tools but cannot present native
  interactive permission prompts. They must report refused operations; use the
  interactive terminal to approve commands and edits. No blanket permission
  bypass was added. Provider sessions remain independent.

## Shared tools and knowledge

Skills live in `~/.claude/skills`. `~/.grok/config.toml` adds no second skill folder.
Grok discovers that Claude library. The local model does not. The instructions in
`~/.grok/AGENTS.md` and `prompts/grok/system_prompt.md` point at the shared system
map, repository instructions, current mandate, wiki, knowledge ledger and memory
references. These are references, not Grok's own past experience.

Configured MCP servers: trading (existing five-tool server), Playwright on the
existing authenticated Chrome CDP session, Context7 and REA. Trading handshake
was verified. These use local interfaces; another provider's proprietary hosted
tools, app grants or subscription entitlements do not transfer to Grok.

## Independent paper trader

`shared/cloud_bots.py` registers Grok with the same active daily prompt and risk
limits as the other cloud books. Its cash begins at $100,000 and it writes only
`data/cloud_grok.json`. No existing book is reset. Protective stops include Grok
through `position_watch.BOT_MODELS`. Research, checkpoints and session reports
use the Grok model key. Lab experiments have the unique `G-` prefix; Lab's paused
timers stay paused, and manual Grok research is available through the existing UI.

The installed and source `cloud-bots.service` include `claude,codex,grok` on the
existing shared snapshot twice per session (operator 2026-11-03).
13:00 America/New_York is the full review, three hours before the 16:00 close.
15:00 is a holdings check only, one hour before the close. An early close
at 13:00 moves those checks to 10:00 and 12:00. The holdings check cannot open
a new position.
Weekend
review automatically includes Grok. Daily books cannot read rival strategy
notes; weekend comparisons expose executed histories and reject copied notes.

`shared/grok_runner.py` separates two runtimes:

1. Engineering: subscription CLI, permitted tools, shared knowledge and an
   independent native session ID per project working directory.
2. Trading/Lab: fresh temporary Grok configuration and working directory, a
   separate daemon socket, native ACP with `--no-leader`, compatibility skills,
   rules, hooks, managed MCPs and memory disabled. A discovery audit refuses
   unexpected active skills, instructions, hooks or MCP servers. Internet search
   is off unless the task demands it. No shell, editing or subagents. Grok opens
   its charts and searches its frozen data with `read_file`, `list_dir` and
   `grep`, like Claude and Codex (operator 2026-11-02). Its `charts/` and `data/`
   are hard-linked into the temporary folder. `shared/grok_sandbox.py` applies
   Landlock before the agent starts, so reads stop at that folder, it is
   read-only, and a failed restriction stops the run. Grok's own `--sandbox` needs
   bwrap, which AppArmor blocks on this host.
   ACP client filesystem/terminal/permission capabilities are refused. The
   existing native login file is referenced for authentication, without copying
   credentials into prompts or reports. No prior conversation is resumed.

The paper trader is pinned to the verified subscription model `grok-4.7` at
medium effort, the same effort as every other Grok session (operator 2026-11-02).
Its reviews use the same batch setting as Claude and Codex.
Set `R2D2_GROK_MODEL` to an available Grok model to override it. Engineering
sessions use the subscription default. No separate API key or paid API billing
was configured.

## Telegram

`grok_bot.py` runs as `telegram-grok-bot.service` using its own `GROK_BOT_TOKEN`.
Run `r2d2-grok-telegram-setup` in a private terminal: it accepts a hidden token
or uses the token already in `.env`, refuses tokens used by another configured
bot, verifies the username using Telegram getMe, saves it with owner-only file
permissions and enables the service. It sends no test messages.

Open the returned t.me link and press Start. Add the bot to the existing Matrix
trading group. Group messages require @4 or the bot username. To receive @4
when Telegram Group Privacy is enabled, disable privacy using @BotFather
/setprivacy, or make the bot a group admin.

Only the configured operator user in the existing personal/trading chat
allowlist may invoke Grok. Other users, other bots and unknown chats are ignored.
Private Telegram messages and Dashboard Chat → Grok share canonical chat -4
and `dashboard/claude-workdir/grok-personal`. Group requests use Grok's separate
group workdir and history. Paper-trading sessions never inherit either chat.
A missing token leaves the installed service inactive until configured.
