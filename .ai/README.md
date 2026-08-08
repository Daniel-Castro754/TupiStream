# AI Orchestrator

This repository uses a manual GitHub Actions workflow to coordinate three AI agents with separate roles:

1. **Codex** implements the requested change in an isolated `ai/run-*` branch.
2. The repository validation commands run (`compileall`, Ruff, Pytest, Docker build).
3. A **draft pull request** is created.
4. **Claude** performs a read-only review and comments on the PR.
5. **Gemini** performs a second read-only review focused on bugs, edge cases, and tests.
6. A human reviews and merges the PR. AI agents never merge to `main`.

## Required repository secrets

Add these in **Settings → Secrets and variables → Actions → New repository secret**:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`

Never commit API keys to the repository.

## GitHub settings recommended before use

Protect `main` with a branch protection/ruleset that requires pull requests and CI checks. Keep auto-merge disabled during the pilot.

## Running the orchestrator

Open **Actions → AI Orchestrator → Run workflow**.

Choose a task type:

- `feature`
- `fix`
- `optimize`
- `refactor`
- `tests`
- `docs`

Then enter the task in the `prompt` field.

Example:

> Improve cache handling without changing the public API. Add regression tests for the changed behavior.

## Safety model

The workflow is intentionally restricted:

- only `Daniel-Castro754` may start the manual run;
- Codex cannot push directly to `main`;
- the workflow rejects Codex changes to `.github/workflows/**` and `.ai/rules.yml`;
- Claude and Gemini are configured as read-only reviewers;
- validation failures do not authorize a merge; the PR remains draft;
- final merge is always a human action.

See `.ai/rules.yml` for the machine-readable policy.
