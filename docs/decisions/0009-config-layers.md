# 0009 — Config layers and the untrusted repo layer

Status: accepted (2026-10-25)

Decision: defaults < `~/.config/dfm/config.toml` (user) < `<repo>/.dfm/config.toml` (repo)
< CLI flags. The user layer is trusted: invalid values or unreadable TOML are a hard
`ConfigError`. The repo layer is untrusted (it ships inside someone else's repository):
- it may only tighten `network.*` (allow < ask < deny), `security.*`,
  `git.preserve_dirty_worktree` and `telemetry.enabled`;
- it may not set anything under `models` (a repo must not pick which weights get loaded);
- rejected, invalid or unreadable repo keys are skipped with a warning, never fatal.
`--offline` sets every network category to deny; `--local-models` sets `remote_inference`
to deny. Config is frozen after load. Hosted providers cannot be expressed:
`models.*.provider` only accepts `mlx`, and `models.apple.remote = true` is rejected.
