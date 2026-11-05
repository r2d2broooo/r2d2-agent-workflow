# Project overview and provenance — 2026-11-05

R2D2 Agents extends the existing project-session page. It reads local Git
repositories, the existing shared task list and token_usage ledger. No new task,
memory, account meter or token collector is introduced. GET never calls a model.
Repository and usage cards refresh at most once a minute; session messages retain
normal polling. Tasks remain the existing shared list; ownership is never guessed.

New sessions select a repository from the local Git repository list. Legacy
sessions retain telegram-bots as their default. Session workdirs still remain
provider-specific. Before every call, the current shared AGENTS contract replaces
the workdir copy and enters the task context. No provider session is reset or
interrupted. The shared contract's revision is recorded on every new call.

A session run records repository, branch, HEAD and working files before and after,
commits/files observed between HEADs, provider, instruction revision, timestamps
and reported models/efforts/token use from the EXISTING ledger. These are observed
changes; concurrent models' commits are not claimed as this session's work. Errors
still record provenance. No source diffs, credentials, private reasoning or second
transcript copy are saved. Legacy runs have no retrospective invented provenance.

Token attribution uses an explicit project root for project calls and the longest
matching repository prefix for existing cwd records, including terminals. Unknown
paths remain unassigned. London calendar days handle DST. The view shows raw
and effective tokens (cache reads weighted at 0.1), never guessed monetary cost.
Yesterday is compared to its preceding seven complete days. An alert requires
three active baseline days, >=1M effective tokens yesterday and >2x the baseline.
Today never contaminates the baseline. Missing counts are explicitly counted.

Unusual usage is visible on the project page and delivered by the EXISTING
 token-watch/token_guard notification path, once per project/date. No new timer,
model call, pause rule, trading blocker or weekly budget is added. Missing local
history is not treated as evidence that usage is anomalous.

Implementation: shared/project_observability.py, agent_projects.py, token_usage.py
and token_guard.py; dashboard agent_project_routes.py and existing Alpine page.
Verification uses temporary databases/token logs and stubbed provider calls.
