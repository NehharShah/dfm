# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning:
[Semantic Versioning](https://semver.org/); before 1.0, any release may contain breaking changes.

## [Unreleased]

### Fixed

- On Python 3.12, a command that left a background process holding its output (for example a
  test run that starts a daemon) blocked until its timeout. DFM now reads output pipes itself,
  so it stops waiting when the command exits on every supported Python version.
- Type checking failed without the optional `mlx` extra.
- The configuration reference split rows whose type contains `|` (such as `int | None`).
- A diagram in `ARCHITECTURE.md` did not render on GitHub.

### Changed

- Live model tests are opt-in through `DFM_TEST_MODEL` and work with any local MLX model, not
  only gpt-oss-20b. Model-logic tests no longer depend on whether `mlx` is installed.
- CI runs on Python 3.12 and 3.13, with actions pinned to commit SHAs.

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
