# R2D2 shared working contract — 2026-11-05

Claude @1, Local Analyst @2, Codex @3 and Grok @4 keep their identities,
provider sessions, tools and paper books independent.

- Follow the operator's latest instructions, then this contract and the applicable
  repository AGENTS.md. Read references only for the task in hand; no mandatory
  history, old handoff, system-map or memory preload.
- No automatic cross-model review, grading or coaching. Ask first unless the
  operator explicitly requested it. cloud-mentor and local-coach stay off.
- Before editing inspect git status, log and diff; preserve other work. Commit each
  finished task locally in every changed repo: `Codex: <change>` (or your identity).
  Never push unless asked. Reading change history is a handoff, not a review.
- Before an audit/eval/report, find the existing result in memory or prompts/docs
  and extend it. Memory is on demand: index/search, then the relevant record only.
  No weekly fold or LLM memory summary. Never preload the full store.
- Verify changes with focused tests using temporary state and stubbed providers.
  Tests must not touch live books, send messages, refresh market inputs or change
  timers. Never fabricate market data, performance or another model's work.
- Keep secrets out of prompts, logs and reports. External/account actions need
  explicit operator authorization. Continue authorized reversible implementation.
- Engineering context never enters isolated paper-trading calls. Those receive
  only the approved prompt, frozen current data and their own book/notes.
- Changes shared by models must update this contract/repo docs and their live
  instruction sources. Existing project workdirs refresh before each new call.

Workspaces: <workspace>/telegram-bots (shared runtime, data, research),
<workspace>/dashboard (FastAPI/Alpine), <workspace>/trading-wiki (research).

Read only when relevant, under telegram-bots/prompts/docs:
- trading_mandate.md: trading behaviour and page contract; current operator
  instructions and executable settings supersede historical entries.
- dashboard_updates.md: later page changes; update alongside page contracts.
- agent_projects_setup.md: project sessions and runtime boundaries.
- project_observability.md: consolidated overview, usage trends and provenance.
- lab_policy.md: deterministic Lab simulation and learned-entry experiments.
- grok_setup.md / model_run_health.md: provider setup / health operations.
Historical instructions are preserved in instruction_archive_2026-11-05;
read only to recover context, never as current rules.
