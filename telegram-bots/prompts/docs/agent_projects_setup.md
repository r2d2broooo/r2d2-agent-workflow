# Project sessions

The dashboard’s **Chat → R2D2 Agents** page creates private Claude and Codex sessions and named team rooms. Sessions keep independent provider workdirs and durable message history. Team rooms collect independent answers from one frozen transcript, then can run a peer-review round. Team discussion proposes work; assign edits in a private session. An unavailable provider is labelled as an error and is never replaced by another model.

Claude, Codex and Grok use their own native CLI authentication. Grok is @4 and has
its own independent sessions and paper book. The provider strip shows local
availability; an actual call can still report a login or capacity error. See
`grok_setup.md` for Grok's terminal, skills and isolated trading runtime.

## Knowledge and permissions

The shared engineering and research workspace is rooted in `<workspace>/r2d2-agents`, with the dashboard and telegram-bots repositories as additional folders. Read the applicable repository’s `AGENTS.md` before editing. Project skills live under `.agents/skills/`.

Every shell command or code edit is presented as a proposal and requires an authenticated operator’s approval. Consent is bound to that exact request and consumed before execution. Denial runs nothing. Background turns survive browser navigation; a dashboard restart marks unfinished turns interrupted and never replays them automatically.

Team rooms preserve labelled contributions and cite evidence. Do not impersonate another agent or claim work another provider did not perform. Keep secrets out of prompts, chat logs and reports. Ask before external account actions.

## Project observability (2026-11-05)

Repository selection, usage trends, anomaly alerts, the existing shared task list
and per-call provenance now live on this same page. See project_observability.md.
The live shared contract is agent_contract.md (also exposed at
<workspace>/r2d2-agents/AGENTS.md). It refreshes before every new project turn.
New team sessions default to no peer review; the operator must explicitly select it.
