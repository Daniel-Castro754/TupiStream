# AI Orchestrator

The orchestrator is intentionally provider-neutral. At each manual run you choose which available AI occupies each role.

## Roles

The workflow exposes three runtime roles:

- **Implementer**: edits the repository in an isolated `ai/run-*` branch.
- **Reviewer 1**: performs a read-only review of the draft PR.
- **Reviewer 2**: optional second read-only review.

Supported providers:

- `codex`
- `gemini`
- `claude`
- `none` for reviewer slots only

The same provider may be used more than once, but independent providers are usually more useful for review.

## Current recommended setup

With only OpenAI and Gemini credentials configured:

- Implementer: `codex`
- Reviewer 1: `gemini`
- Reviewer 2: `none`

If OpenAI quota or pricing becomes undesirable, you can switch the implementer to `gemini` for a run. If an Anthropic credential is added later, `claude` becomes available in any role without redesigning the workflow.

## Repository secrets

Add only the providers you actually use in **Settings → Secrets and variables → Actions**:

- Codex: `OPENAI_API_KEY`
- Gemini: `GEMINI_API_KEY`
- Claude: `ANTHROPIC_API_KEY`

A missing secret does not break providers that were not selected. If a role selects a provider whose credential is missing, that role fails early with a clear error.

Never commit API keys to the repository.

## Optional model variables

The provider can also be kept while changing the exact model independently of the workflow file. Under **Settings → Secrets and variables → Actions → Variables**, you may define:

- `AI_CODEX_MODEL`
- `AI_GEMINI_MODEL`
- `AI_CLAUDE_MODEL`

Leave a variable absent/empty to use that provider action's default model. This makes it possible to switch to a cheaper/faster model later without editing the workflow.

## Running the orchestrator

Open **Actions → AI Orchestrator → Run workflow** and choose:

1. task type;
2. implementer;
3. reviewer 1;
4. reviewer 2;
5. prompt.

Example low-risk pilot:

- Task type: `tests`
- Implementer: `codex`
- Reviewer 1: `gemini`
- Reviewer 2: `none`
- Prompt: `Analyze the current tests and add one useful regression test without changing production code.`

## Execution flow

1. Validate only the credential required by the selected implementer.
2. Create an isolated `ai/run-*` branch.
3. Run the selected implementer.
4. Reject any AI modification under `.github/**` or `.ai/**`.
5. Run `compileall`, Ruff, Pytest, and Docker build.
6. Commit the implementation using the GitHub Actions bot and push the isolated branch.
7. Open a **draft pull request**.
8. Run each selected reviewer in read-only mode.
9. Keep the PR as draft for human review and merge.

## Safety model

- only `Daniel-Castro754` may start the manual implementation workflow;
- no AI provider is allowed to merge to `main`;
- agents cannot change the orchestration policy or workflow during implementation;
- reviewers are read-only;
- provider credentials are supplied only to the step that needs them;
- validation failures never authorize a merge;
- final merge is always a human action.

See `.ai/rules.yml` for the machine-readable policy.
