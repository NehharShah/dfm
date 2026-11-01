# 0010 — Sandbox boundary for repository code

Status: accepted (2026-10-26)

Context: policy classifies command lines, but tests, build scripts and package hooks are
repository code that can do anything once started.

Decision: every command DFM runs goes through `CommandRunner`, which can wrap it with a per-run
macOS `sandbox-exec` profile (`dfm/security/sandbox.py`):
- deny reads and writes in protected home locations (same list as the filesystem boundary);
- allow writes only to the repository, DFM state dirs, the session temp dir, system temp dirs and
  known toolchain caches (cargo, npm/pnpm/yarn/bun, go, uv, gradle/maven, swiftpm, foundry, scarb);
- deny outbound network unless the command was authorized for a network category (and never
  offline); loopback and Unix sockets stay open for local services.

Consequences: `sandbox-exec` is deprecated but present on macOS 27; if Apple removes it the wrap
becomes a no-op and doctor warns (env scrubbing still applies). System temp dirs remain writable.
Linux would need a different backend (bubblewrap/landlock); not in scope.

Addendum (2026-10-31, whole-codebase review): the write allowlist was too wide in three places
and the read denylist too narrow in one. Repository code may no longer write `<repo>/.dfm`
(checkpoint originals that `/undo` restores, repository policy), `~/.cache/dfm` (the persisted
KV cache the worker restores as the model's prefix; the worker's own profile re-allows its `kv`
directory) or DFM's own interpreter and venv (`sys.prefix`, `sys.base_prefix`, which sit under
the uv toolchain cache). It may no longer read shell startup files and histories, other coding
agents' state (`~/.claude`, `~/.codex`, `~/.cursor`), the Hugging Face token, or DFM's policy,
traces, logs, metrics, registry and caches (`ExecutionEnvironment.private_state`); model weights
under `~/.local/share/dfm/models` and the session temp dir stay readable. Network for a compound
command is granted only when every part was approved: read-only tools that can start programs
(`sed`, `awk`, `less`, `find`, `git`, editors) no longer count as "no code".
