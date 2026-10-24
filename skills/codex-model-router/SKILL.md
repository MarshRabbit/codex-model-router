---
name: codex-model-router
description: Route multi-step coding work to Codex model and reasoning profiles, verify each result independently, and retry failed work at a higher tier. Use when the user requests automatic model selection, cost-aware delegation, or a verified coding workflow.
---

# Codex model router

The user's chosen scope, model, and permissions take priority. This skill is a
Codex adaptation of danweinerdev/claude-model-router. Its Python supervisor enforces
the verification/retry stages; native custom-agent prompts alone do not.

If `CODEX_ROUTER_ACTIVE=1`, you are already a worker: perform the bounded task
directly and return the requested JSON. Do not start the runner recursively.

For a known one-step operation, use the deterministic tool directly. Otherwise:

- **Inside Codex:** read [the native transport procedure](references/native.md).
  Use Codex's native subagents and the persistent supervisor. This avoids a nested
  CLI sandbox and still runs actual checks before accepting results or retrying.
- **From a regular terminal/CI outside a Codex sandbox:** the single-command CLI
  transport below runs `codex exec` itself. If nested sandbox initialization fails,
  use the native transport; do not disable sandboxing or repeat the failed run.

1. Identify the actual project root. Use `scripts/router.py routes --project PATH`
   to inspect the model/effort profiles and configured checks. Resolve scripts
   relative to this skill directory, not the shell's current directory.
2. Use the existing `.codex/model-router.json` checks. If absent, select relevant
   acceptance commands from the project and add them with repeatable `--check`
   arguments. These are trusted argv commands, not shell pipelines. Do not invent
   a success check that merely matches the implementation. Tests execute locally
   with the invoking user's permissions, so stay within the user's authorized scope.
3. Run `python3 <skill-dir>/scripts/router.py run --project PATH --task-file FILE`.
   Put the full user objective, acceptance criteria, and file ownership in FILE.
   `--agent builder` bypasses decomposition for a well-scoped implementation;
   `--tier` explicitly selects its initial model/effort profile.
   For a requested specific model, use a matching configured profile, or add a
   project profile for that model before running. Do not silently substitute it.
4. Wait for the actual process. Inspect `summary.json` and its check/review logs.
   `completed` requires successful configured checks AND an independent read-only
   review for every task. With no commands, the evidence is review-only: say so.
   A worker's `checks_reported`, confidence, or escalation marker is not evidence.
5. Report the resulting changes, verification, and remaining issues. Preserve
   useful edits after failure. Stop at `needs_input`, exhausted budget, or a failed
   final tier; do not quietly restart the entire run and reset its limits.

The runner selects ordered model/effort profiles, keeps the same specialist on
retry, and hands actual failed command output/reviewer findings to the next
attempt. It does not edit global Codex configuration or bypass sandboxing.
Native `router_*` custom agents may also be used for manual delegation without
the supervisor, but then the parent owns verification. Do not claim the enforced
supervisor was used for a native-only run. Native subagents inherit permissions;
their read-only role instructions are not an independent OS sandbox.
