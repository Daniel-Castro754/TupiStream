# AI Orchestrator

The orchestrator is provider-neutral. At each manual run you choose which available AI occupies each role, and the workflow is designed so that failures after implementation can be retried without paying for the implementation model again.

## Roles

The main workflow exposes three runtime roles:

- **Implementer**: edits the repository in an isolated `ai/run-*` branch.
- **Reviewer 1**: performs a read-only review of the draft PR.
- **Reviewer 2**: optional second read-only review.

Supported providers:

- `codex`
- `gemini`
- `claude`
- `none` for reviewer slots only

The same provider may be used more than once. Independent providers can add diversity to review, but using a single available provider is fully supported.

## Current recommended setup

When Codex is the available provider:

- Implementer: `codex`
- Reviewer 1: `codex`
- Reviewer 2: `none`

For lower token usage, set both reviewer slots to `none` and perform the final review manually.

Gemini and Claude remain optional choices. They can be selected again later without redesigning the workflow whenever their credentials, quota, or pricing make sense.

## Repository secrets

Add only the providers you actually use in **Settings → Secrets and variables → Actions**:

- Codex: `OPENAI_API_KEY`
- Gemini: `GEMINI_API_KEY`
- Claude: `ANTHROPIC_API_KEY`

A missing secret does not break providers that were not selected. If a role selects a provider whose credential is missing, that role fails early with a clear error.

Never commit API keys to the repository.

## Optional model variables

Under **Settings → Secrets and variables → Actions → Variables**, you may define:

- `AI_CODEX_MODEL`
- `AI_GEMINI_MODEL`
- `AI_CLAUDE_MODEL`

Leave a variable absent/empty to use that provider action's default model. This lets you change price/performance without editing workflow code.

## Running a new task

Open **Actions → AI Orchestrator → Run workflow** and choose:

1. task type;
2. implementer;
3. reviewer 1;
4. reviewer 2;
5. prompt.

Example low-risk setup:

- Task type: `tests`
- Implementer: `codex`
- Reviewer 1: `codex`
- Reviewer 2: `none`

## Resumable execution

The workflow deliberately separates expensive model work from GitHub publishing/review stages.

### Stable branch per workflow run

A new task uses a deterministic branch:

`ai/run-<GITHUB_RUN_ID>`

If the workflow is retried and that branch already exists remotely, the implementation stage reuses it and **does not call the implementation model again**.

This is especially useful when:

- GitHub temporarily fails while creating the PR;
- PR permissions were misconfigured;
- a reviewer API is unavailable;
- a reviewer runs out of quota;
- a later GitHub step fails.

### PR publishing is its own job

Implementation and PR creation are separate jobs. If PR creation fails after the implementation branch was pushed, use **Re-run failed jobs** in GitHub Actions. The successful implementation job is preserved and the model is not rerun.

The publish job is also idempotent: if an open PR already exists for the workflow branch, it reuses that PR instead of creating a duplicate.

### Review deduplication

Each AI review comment contains a hidden marker tied to:

- PR head commit SHA;
- reviewer slot;
- provider.

Before invoking a reviewer, the workflow checks for that marker. If the same provider already reviewed the same commit in that slot, the model is skipped. This avoids duplicate token spending on retries.

If the PR receives a new commit, its SHA changes and a fresh review can run.

## Reviewing or resuming an existing PR

Use **Actions → AI Review Existing PR → Run workflow** when implementation is already complete and you only want AI review.

Inputs:

1. PR number;
2. reviewer 1;
3. reviewer 2.

Example:

- PR number: `8`
- Reviewer 1: `codex`
- Reviewer 2: `none`

This workflow never runs an implementer. It only reviews the existing PR and therefore is useful after partial failures or when you intentionally want to change reviewers later.

For security, this manual review workflow only accepts PR branches that belong to this same repository. It refuses fork PRs before exposing provider credentials.

## Execution flow for a new task

1. Checkout the repository.
2. Create or recover the deterministic `ai/run-*` branch.
3. If the branch is new, validate only the credential required by the selected implementer.
4. Run the selected implementer.
5. Reject any AI modification under `.github/**` or `.ai/**`.
6. Run `compileall`, Ruff, Pytest, and Docker build.
7. Commit with the GitHub Actions bot and push the isolated branch.
8. In a separate job, create or recover a **draft pull request**.
9. Run each selected reviewer in read-only mode, skipping reviews already completed for the same commit/provider/slot.
10. Keep the PR as draft for human review and merge.

## Recommended retry strategy

If a later stage fails:

1. Prefer **Re-run failed jobs** rather than re-running the entire workflow.
2. If the PR already exists and you only need reviews, use **AI Review Existing PR**.
3. If a provider is unavailable or out of quota, choose another provider explicitly instead of using automatic fallback. This keeps API spending predictable.
4. Do not start a brand-new AI Orchestrator run merely to recover a publishing or review failure; a new run receives a new `GITHUB_RUN_ID` and is treated as a new implementation task.

## Safety model

- only `Daniel-Castro754` may start the manual workflows;
- no AI provider is allowed to merge to `main`;
- agents cannot change `.github/**` or `.ai/**` during implementation;
- reviewers are read-only;
- provider credentials are supplied only to the selected provider step;
- review retries are deduplicated per commit/provider/slot;
- the existing-PR reviewer refuses PRs from forks;
- validation failures never authorize a merge;
- final merge is always a human action.

See `.ai/rules.yml` for the machine-readable policy.
