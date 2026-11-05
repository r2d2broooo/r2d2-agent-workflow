# Standalone use

Use an isolated virtual environment, then `pip install -r requirements.txt`.
`PYTHONPATH=telegram-bots python -m shared.token_usage --hours 24` reads usage.
Session memory is available from shared.session_memory; use R2D2_SESSION_MEMORY to
choose its private storage. `R2D2_WORKSPACE` overrides the exported workspace root.
Provider CLIs need their own login; no credentials or CLI executables are included.
Optional API: set R2D2_API_KEY to a local secret and run `uvicorn serve:app --host
localhost --port 8430`. It uses an x-api-key header. Never expose it unauthenticated.
Create an ordinary local Git repo under this workspace before choosing it as a
project. The source integration's telegram-bots fallback is only a compatibility
default; explicitly select the cloned repo/project you intend to operate on.
No trading engine, market data, Lab or order functions are imported by this toolkit.
