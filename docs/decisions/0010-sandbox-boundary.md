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
