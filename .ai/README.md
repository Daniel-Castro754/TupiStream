# AI Orchestrator

The repository supports multiple AI providers while keeping implementation branches isolated, reviews read-only, and final merge human-controlled.

## Providers

Supported credentials:

- Codex: `OPENAI_API_KEY`
- DeepSeek: `DEEPSEEK_API_KEY`
- Gemini: `GEMINI_API_KEY`
- Claude: `ANTHROPIC_API_KEY`

Optional model variables under **Settings → Secrets and variables → Actions → Variables**:

- `AI_CODEX_MODEL`
- `AI_DEEPSEEK_MODEL`
- `AI_GEMINI_MODEL`
- `AI_CLAUDE_MODEL`

Missing credentials only affect a role/provider that was actually selected.

## Current recommended setup

While Gemini quota is unavailable, the normal orchestrator recommendation is:

- Implementer: `codex`
- Reviewer 1: `codex`
- Reviewer 2: `none`

No automatic provider fallback is used, so an API failure cannot unexpectedly spend credits with another provider.

## DeepSeek integration

DeepSeek is available through **Actions → AI DeepSeek Task**.

This workflow can:

1. use DeepSeek as the implementation agent;
2. run the repository validation suite;
3. create a draft pull request;
4. optionally use `codex`, `deepseek`, or `none` as the reviewer;
5. keep merge as a human-only decision.

DeepSeek runs through `.ai/deepseek_agent.py`. The agent uses the official OpenAI-compatible DeepSeek API and a deliberately small set of repository tools:

- read file;
- list directory;
- glob paths;
- text search;
- write file;
- exact text replacement.

It has no shell tool and no generic network tool. Write operations are blocked for `.github/**` and `.ai/**`, and dotenv files are not exposed through its read tool.

The default model is `deepseek-v4-flash`. Set `AI_DEEPSEEK_MODEL=deepseek-v4-pro` if you intentionally want the higher-cost model.

### Recommended first DeepSeek test

- Task type: `optimize`
- Reviewer: `codex`
- Prompt: ask for one small, low-risk improvement with tests and no public API change.

This tests two independent roles:

`DeepSeek implements → CI validates → Codex reviews → human decides merge`

## Running the normal orchestrator

Open **Actions → AI Orchestrator → Run workflow** and choose:

1. task type;
2. implementer;
3. reviewer 1;
4. reviewer 2;
5. prompt.

The main workflow remains provider-neutral for Codex/Gemini/Claude and is resumable after later-stage failures. DeepSeek is initially isolated in its dedicated workflow so its custom tool-calling agent can be validated without destabilizing the existing orchestrator. After it proves reliable, it can be folded into the common provider selector.

## Resumable execution

A new orchestrator task uses a deterministic branch such as:

`ai/run-<GITHUB_RUN_ID>`

The DeepSeek workflow similarly uses:

`ai/deepseek-<GITHUB_RUN_ID>`

If a workflow is retried and its implementation branch already exists remotely, the implementation stage reuses it and does **not** call the implementation model again.

This is useful when GitHub temporarily fails while creating a PR, a reviewer API is unavailable, a reviewer runs out of quota, or a later GitHub step fails.

## Review deduplication and recovery

The normal AI Orchestrator includes review deduplication tied to PR head SHA, reviewer slot, and provider. It also provides **AI Review Existing PR** so a PR can be reviewed again with another provider without rerunning implementation.

Prefer **Re-run failed jobs** when recovering a later-stage failure instead of starting a brand-new implementation run.

## Safety model

- only `Daniel-Castro754` may start the manual workflows;
- no AI provider is allowed to merge to `main`;
- agents cannot change `.github/**` or `.ai/**` during implementation;
- DeepSeek does not receive shell or generic network tools;
- reviewers are read-only;
- provider credentials are supplied only to the selected provider step;
- the existing-PR reviewer refuses PRs from forks;
- validation failures never authorize a merge;
- final merge is always a human action.

See `.ai/rules.yml` for the machine-readable policy.
