# R2D2 Agent Workflow

A reusable toolkit for operating independent AI assistants while keeping session history, memory retrieval, resource use and project provenance explicit. It extracts the general agent infrastructure from the larger R2D2 workspace and can be used without the trading engine.

It is useful when several providers share an engineering workspace but should retain their own identities, authentication, native sessions and records. It does not turn them into one interchangeable assistant.

## What it provides

- **Project sessions:** named conversations with separate provider workdirs, durable messages and restart/interruption handling.
- **Optional team discussion:** independent answers from a common transcript. A further review round is an explicit operator choice, not an automatic requirement.
- **On-demand memory:** verbatim session/report storage with searchable handles. Retrieval happens when needed; a new prompt does not receive the entire archive.
- **Token accounting:** records reported input, output and cache usage, provider/model/effort labels and terminal usage where supported.
- **Usage controls:** existing limit/error handling and unusual-use notices. Token counts and weighted “effective” counts are operational measures, not an invoice.
- **Project visibility:** repository context, usage trends and records of the branch, commit state and instruction revision observed around a call. Concurrent changes are labelled as observations rather than attributed to a model without evidence.
- **Shared infrastructure:** a provider registry, secret-redaction helpers and optional Telegram notices.

## Structure

The reusable modules live under `telegram-bots/shared/`; the directory name preserves compatibility with the original integration. There is no market-data, Strategy Lab or order engine in this toolkit. `serve.py` exposes the existing project-session routes as a small optional API.

## Setup and basic use

Use Python 3.11 or newer and a dedicated environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
python3 check.py
PYTHONPATH=telegram-bots .venv/bin/python -m shared.token_usage --hours 24
```

Choose private storage with `R2D2_SESSION_MEMORY`, `R2D2_AGENT_STATE_DIR`, `R2D2_TOKEN_LOG` and `R2D2_TOKEN_GUARD` as needed. `R2D2_WORKSPACE` can override the workspace root. Configure the provider CLIs independently before requesting a model response.

For the optional API, set `R2D2_API_KEY` locally, then run:

```bash
.venv/bin/python -m uvicorn serve:app --host localhost --port 8430
```

Requests use the `x-api-key` header. The default API denies requests when no key is configured. See [USAGE.md](USAGE.md) for details.

## Boundaries

Creating/listing a session or inspecting usage does not itself require inference. Sending a session message can invoke the selected provider and incur its normal usage. Account access and provider execution are not tested by syntax checks. This release includes no saved conversations, credentials, account meters or native provider sessions. It is an extraction with documented adapters, not a universal agent framework or a provider subscription.

## Privacy, provenance and updates

This repository is a sanitized source export. No provider credentials, personal contact information, configured IP addresses, private conversations or runtime databases are included. Example hostnames and local environment variables are intentional. Keep newly generated account data and captures outside version control.

A source manifest records exported files. Local adaptations and third-party material retain their own provenance/notices; public visibility does not apply a new blanket license to every file. Related repositories and the manual update process are listed in the [workspace catalogue](https://github.com/r2d2broooo/r2d2-workspace-catalog).

Updates are made only on explicit request. Cloning does not enable an automatic sync, scheduled publication or Git push.
