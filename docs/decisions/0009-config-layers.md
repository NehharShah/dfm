# 0009 — Config layers and the untrusted repo layer

Status: accepted (2026-10-25)

Decision: defaults < `~/.config/dfm/config.toml` (user) < `<repo>/.dfm/config.toml` (repo)
< CLI flags. The user layer is trusted: invalid values or unreadable TOML are a hard
`ConfigError`. The repo layer is untrusted (it ships inside someone else's repository) and is
enforced as an allowlist (`dfm/config/loader.py`, `_repo_may_set`):
- it may tighten `network.*` (allow < ask < deny);
- it may move a boolean toward its safe value and never away: `security.*` toward the
  protective setting, `integrations.*.enabled`, `integrations.reflex.session_export`,
  `use_skills`, `use_handbook` and `git.prefer_worktree_for_large_tasks` toward `false`;
- it may lower `limits.*` and never raise them;
- every other key is refused, including anything under `models` (a repo must not pick which
  weights get loaded);
- rejected, invalid or unreadable repo keys are skipped with a warning, never fatal.

Amended 2026-10-31: the first implementation was a denylist (only `network.*` and a few
booleans were checked), so a repository could raise `limits.*`, re-enable Reflex export or
grant worktree consent. Keys nothing read (`*.required`, `repokg.incremental`,
`reflex.use_routing_history`, `models.*.lazy_load`, `security.redact_secrets`,
`security.allow_home_directory_reads`, `git.preserve_dirty_worktree`, `telemetry.enabled`) were
removed rather than documented as settings: redaction, the protected-path denylist and the Git
guard are always on, and DFM has no telemetry to switch.
`--offline` sets every network category to deny; `--local-models` sets `remote_inference`
to deny. Config is frozen after load. Hosted providers cannot be expressed:
`models.*.provider` only accepts `mlx`, and `models.apple.remote = true` is rejected.
