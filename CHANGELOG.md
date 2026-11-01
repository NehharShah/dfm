# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning:
[Semantic Versioning](https://semver.org/); before 1.0, any release may contain breaking changes.

## [Unreleased]

### Added

- `!command` in the interactive session runs a command yourself, through the same policy and
  sandbox as the model's commands.
- `limits.task_minutes` (default 15): a task that runs longer stops between steps and reports
  what it verified so far.

### Fixed

- Questions in the interactive session (including `ls`) were answered from a repository summary
  only. The model now looks at the repository to answer: it reads, searches and runs read-only
  commands, and never edits.
- `dfm` started a session in the home folder and treated everything under it as the repository.
  It now refuses the home folder, its parents and `/`, and warns outside a Git repository.
- The session banner showed the model's Hugging Face snapshot hash instead of its name.
- Typing `exit` or `quit` in the interactive session sent it to the model as a question; it now
  ends the session, like `/exit`.
- Keys typed while a request ran were echoed over its output; they now wait at the next prompt.
- A test tool that is not installed (for example `pytest` outside a virtual environment) was
  reported as a failed run inside the macOS sandbox (exit 71), and the coder spent its steps
  looking for the tool. DFM now reports "`pytest` is not installed here", tells the coder not to
  look for it or install it, and lists the tests as not verified.

## [0.0.3] - 2026-10-31

### Added

- `dfm` in a terminal starts an interactive session (also `dfm chat`). The local model loads once
  and stays loaded between requests; each step is shown as it runs, with the diff of every
  edit; confirmations appear inline. Requests that change code run the `dfm implement` loop,
  questions are answered read-only, and review requests review the working-tree diff.
  Commands: `/diff`, `/undo`, `/plan`, `/review`, `/ask`, `/do`, `/status`, `/clear`, `/help`,
  `/exit`. Ctrl-C stops the current request without ending the session (ADR 0015).
- `/undo` reverts the files DFM changed in its latest request, skipping any file changed since.

### Changed

- Without a terminal, `dfm` lists the commands instead of saying the session is not built.
- New dependency: `prompt-toolkit`, for the session's input line.
- CI tests Python 3.12 and 3.15, the oldest supported version and the newest.

### Fixed

- On Python 3.14 and later, the configuration reference showed types such as `int | None` as
  `Union`.

## [0.0.2] - 2026-10-31

### Fixed

- The README (and so the PyPI project page) showed a package-install quick start; the checkout
  quick start is back, with `uv tool install dfm-agent` mentioned in one line.
- On Python 3.12, a command that left a background process holding its output (for example a
  test run that starts a daemon) blocked until its timeout. DFM now reads output pipes itself,
  so it stops waiting when the command exits on every supported Python version.
- Type checking failed without the optional `mlx` extra.
- A versioned Xcode install (`/Applications/Xcode_16.4.app`, as on CI runners and machines
  with several Xcodes) was treated as outside the repository boundary, so policy refused
  `swift test` with the Swift Testing plugin path.
- Service and Docker availability checks looked up binaries on DFM's own `PATH` rather than
  the one the command then runs with.
- The configuration reference split rows whose type contains `|` (such as `int | None`).
- A diagram in `ARCHITECTURE.md` did not render on GitHub.

### Changed

- Live model tests are opt-in through `DFM_TEST_MODEL` and work with any local MLX model, not
  only gpt-oss-20b. Model-logic tests no longer depend on whether `mlx` is installed.
- CI runs on Python 3.12 and 3.13, with actions pinned to commit SHAs.
- The package is distributed as `dfm-agent` (the command stays `dfm`); install with
  `uv tool install dfm-agent`. RepoCanon and RepoKG are released dependencies with
  version bounds instead of pinned Git commits, the sdist contains only what the package ships,
  and a tag push publishes to PyPI through trusted publishing (ADR 0014).

## [0.0.1]

First public release.

### Added

- Commands: `ask`, `plan`, `implement` (optionally in a worktree), `resume`, `review`,
  `security-review`, `ci`, `fix-pr`, `review-pr`, `implement-issue`, `issue`, `pr`, `comments`,
  `benchmark`, `doctor`, `setup`, `models`, `repo`, `diff`, `inspect-log`, `sessions`, `status`,
  `profile`, `processes`, `services`, `memory`, `config`.
- On-device models: Apple FM through the `fm` CLI, and MLX models in a sandboxed worker process,
  behind one interface with fallback and circuit breakers.
- Deterministic routing with an Apple FM tie-breaker and memory-aware model selection.
- Agent loop: one action per step, native tool calls, per-step reasoning effort, thinking and
  task token budgets, history compaction, repair, and a verification ledger.
- Safeguards: command classifier, policy engine with typed confirmations, per-command macOS
  sandbox, secret redaction, prompt-injection handling, Git guard and hardening, filesystem
  boundary with checkpoints.
- Resource controls: memory admission, thermal pacing, loop cutoff, and a KV cache reused across
  steps and sessions.
- Integrations: RepoCanon, RepoKG, Reflex, GitHub (`gh`), Docker and Compose, Kubernetes, LSP.
  Language support: Python, TypeScript, Rust, Go, Swift, Solidity, Cairo.
- Versioned JSONL session traces, checkpoints and recovery, per-session metrics.
- Documentation: guide, architecture, command and configuration references, policy, threat
  model, troubleshooting, ADRs 0001–0013.
