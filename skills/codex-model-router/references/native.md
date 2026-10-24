# Native Codex transport

Use this transport inside Codex, especially macOS where a nested `codex exec`
can fail with `sandbox-exec: sandbox_apply: Operation not permitted`.
The parent dispatches real Codex subagents. The Python controller runs checks,
stores evidence, enforces stages/tickets/attempt budgets, and chooses retry profiles.
Never switch to a less restricted sandbox to work around a denied operation.

1. Read `scripts/router.py routes --project PATH`. Decompose the request into a
   short sequential plan with fields `summary`, `tasks`. Each task has `id`,
   `agent`, `tier`, `instructions`, `depends_on`. IDs are lowercase identifiers;
   dependencies refer only to earlier tasks. Match capability and role minimum
   tier. Keep high-risk reasoning at deep, not an unverified cheap implementer.
   For one scoped task, use `--agent builder` (or the relevant registered role)
   instead of a plan file. Known one-shot deterministic work can run directly.
2. Start the supervisor:

   ```text
   python3 <skill-dir>/scripts/router.py native start --project PATH --agent builder --task-file REQUEST --check 'python3 -m unittest discover -s tests'
   ```

   Or replace `--agent` with `--plan-file PLAN`. Checks must be trusted commands
   from the project/user. Initial ceiling needs explicit `--agent ... --tier ceiling`.
3. The response contains `run_id`, `status`, and an `action` with stage, ticket,
   model, reasoning_effort, read_only, prompt_file, and schema_file. Read the prompt
   file and dispatch exactly one native subagent with that bounded assignment.
   Use the tool's explicit model and reasoning settings where supported. With
   `spawn_agent`, use `fork_turns="none"` when model/effort overrides require it;
   supply `model=action.model`, `reasoning_effort=action.reasoning_effort`, and a
   neutral/default agent type so another custom role does not override them.
   Otherwise use a matching project `router_*` custom agent or explain unsupported
   settings; do not silently claim a model/effort setting you could not select.
4. Wait for that subagent. Write its complete structured JSON result to a result
   file without revising its status/findings. Submit the worker result with its
   actual agent ID and the original dispatch ticket:

   ```text
   python3 <skill-dir>/scripts/router.py native worker-result --project PATH --run-id RUN --ticket TICKET --agent-id ACTUAL_AGENT_ID --result-file RESULT
   ```

   The controller executes approved checks itself and issues a reviewer action.
5. Spawn a **separate** subagent for that reviewer action. Respect read_only and
   preserve an independent context; do not substitute the worker's confidence or
   your own rewritten judgement. Submit its JSON through `native review-result`
   using the review ticket and reviewer agent ID. The controller rejects an ID
   equal to the current worker's. A failed check cannot be overridden by approval.
6. Follow the returned action until completed or stopped. A verified implementation
   failure creates the next worker action at a higher model/effort profile with
   actual check output and reviewer feedback. Environment/requirements failures
   stop. Do not start a new supervisor to reset an exhausted budget.

`native status --project PATH --run-id RUN` retrieves the outstanding action
without consuming another dispatch. Each result ticket is consumed once.
`native cancel --project PATH --run-id RUN` releases the active run. First stop
any still-running native subagent with the available interrupt tool. This
controller cannot interrupt a live Codex subagent itself.

Native subagents inherit the parent's permissions; read_only is a role contract,
not a separate OS sandbox in this transport. Their actual reported IDs and
requested profiles are logged, but the controller cannot authenticate IDs or
inspect the parent's live tool calls. Do not misrepresent hand-written results
as real subagent executions. Native tokens/costs are not inferred from dispatches.
Read-only reviews and deterministic tests are evidence, not a proof of correctness.
