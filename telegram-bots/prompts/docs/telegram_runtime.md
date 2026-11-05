# Telegram and shared group runtime — 5 November 2026

Four independent services remain: Claude @1, Local Analyst @2, Codex @3 and Grok @4.
Their provider runners, native sessions, private histories and workdirs remain separate.
Telegram personal chats still use the same canonical conversations as the dashboard.

## Shared message handling

`shared/telegram_runtime.py` owns environment parsing, sender checks, mention handling,
group-message recording, best-effort typing indicators and token-safe error logging.
Each bot retains its provider-specific analysis and response pipeline.

- Normal agent chats accept the configured operator in the configured personal/trading chats.
  Bot senders are ignored, including when they mention an agent.
- Group replies require that agent's registry alias or exact bot username. Email fragments
  and partial handles do not count. Unaddressed messages do not send typing indicators.
- Routing aliases are removed before intent detection. Supervisor instructions keep references
  to other agents so explicit coordination requests retain their meaning.
- All receivers record the original group event with the same
  `telegram:<source chat id>:<message id>` source key. SQLite's unique partial index stores it
  once, even across processes. Identical text with a new Telegram message id is a new message.
  This deduplicates shared history, not the independently addressed agents' replies.
- `messages.source_key` is additive and defaults to an empty string. Existing message rows
  and ordinary dashboard writes are preserved. Existing historical duplicates are not deleted.
- `shared/group_chat.supervisor_prompt` supplies one current-roster contract to Claude,
  Codex and Grok from both Telegram and the dashboard. Recent history is labelled as context;
  the current operator instruction is explicit. A 20-message request reads 20 rows, not 1,000.
- Typing failures do not abort a request. Local replies use the same full-text splitter as
  the cloud bots, and a missing chart does not discard the written answer.
- Local `/reset` is operator-only and ignores commands addressed to another bot.

Codex's feedback/PZ room remains the explicit exception: members can use the existing bounded
server commands, confirmations and Workshop flow. Arbitrary supervisor work remains
operator-only. A separately configured PZ operations room now reaches that same branch.
Neither the allowlist nor the supported server operations were expanded.

## Validation and operation

Regression coverage: `tests/test_telegram_routing.py`, `tests/test_grok_telegram.py`,
`tests/test_codex_feedback_group.py` and dashboard `tests/test_group_chat_routing.py`.
Tests use temporary databases and fake provider/network boundaries; no live message is sent.
Run the full suites from their respective directories:

```sh
venv/bin/python -m pytest
```

The durable source snapshots and task-only patch are under
`~/backups/telegram-simplification-20261105/source-before`, `source-after` and `changes.patch`.
A private SQLite online backup is at
`~/backups/telegram-simplification-20261105/chat_log.before.sqlite3`.
The additive migration was rehearsed on a copy, preserving all existing message fields and
table counts and passing SQLite integrity checking. Restart the four Telegram services and
dashboard only when no active provider turn, local request or project turn would be interrupted.
Do not use the old dashboard `deploy.sh --all` for this rollout: it restarts only two bots.

For code rollback, restore only this task's edits using the saved source copies/patch and restart
the affected services when idle. The extra database column/index are compatible with the
previous code and can stay. Do not restore an old database over newer conversations.

This change does not alter paper-trading rules, books, schedules, model choices or provider
failover behaviour.

## Rollout evidence

Full suites: 926 telegram-bots tests and 129 dashboard tests passed. Three provider-test
doubles also failed on the pre-task baseline because they lacked the runner's existing
`effort` keyword; the doubles now accept it and verify the forwarded effort.

All four Telegram agents and the dashboard restarted while idle on 5 November at 10:57 GMT.
Live Telegram getMe/getWebhookInfo/getChatMember checks authenticated all four bot identities,
matched their configured usernames and confirmed polling mode with no queued updates.
All services reached application startup with zero restart loops; dashboard HTTP returned 200.
The live database has the additive source-key index and passes integrity checking, with all
871 existing messages preserved. No synthetic chat or model request was sent to production.

**External delivery issue resolved:** the initial live check found @Cdx23_Bot with Group
Privacy enabled as a regular member of Matrix. After the operator's Telegram changes, fresh
getMe checks report Group Privacy disabled for Codex, Local and Claude. Grok remains an
administrator in Matrix, so it can receive numeric aliases despite its global privacy setting.
The application does not relay or impersonate Codex. Telegram Web was not authenticated here;
no Telegram login material was read or copied.

The changed-file secret scan passed. The whole-tree scan reported two unchanged false
positives: the archived UI label `token_budget:'Token-budget repair'` and the hidden-input
expression in `scripts/setup_grok_telegram.py`. Neither finding contains a credential.
