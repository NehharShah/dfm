# Troubleshooting

Run `dfm doctor` first. It checks models, tools, integrations and policy without network
access.

| Symptom | Cause and fix |
|---|---|
| `mlx-lm not installed` | The local coder needs the `mlx` extra: `uv tool install "dfm-agent[mlx]"` (or `uv sync --extra mlx` in a checkout). Without it DFM answers questions but cannot edit. |
| `not enough memory for a local coder (N GB available after headroom)` | The model does not fit in free memory. Close large apps, or choose a smaller model with `dfm setup`. A 20B model needs about 13 GB plus headroom. |
| `no coding model available (run dfm setup …)` | No local coder is configured. Run `dfm setup`. |
| Apple FM unavailable | `fm available` must report the system model as ready (Apple Intelligence enabled). Without it, DFM routes by rules and uses the local coder for text. |
| `refused by policy: … not approved` in a non-interactive run | Without a terminal, ask means deny. Run interactively, or allow the category in `~/.config/dfm/config.toml` ([policy](policy.md)). |
| A plain command asks: `environment prefix may change what runs: FOO` | `FOO=1 cmd` asks unless `FOO` is on the benign list (`CI`, `NO_COLOR`, `LC_*`, …), because `PATH=`, `GIT_*` and `*_OPTIONS` prefixes can make a read run something else. Approve it, or drop the prefix. |
| `cd without a path leaves the repository`, `alias … changes how the shell resolves later commands` | Bare `cd` goes to `$HOME`; `alias`/`export`/`hash`/`set` change what later commands mean. Both ask. Name the directory; set variables in your own shell. |
| `… ignored, a repository cannot set this setting` or `may only tighten` | `<repo>/.dfm/config.toml` is an allowlist: tighten network, turn things off, lower limits. Put anything else in `~/.config/dfm/config.toml`. `dfm config show` lists each value's source. |
| `missing python package X: uv add X` | DFM detected a missing dependency and asks before installing. Offline, it only reports it. |
| RepoKG unavailable; structural impact not analyzed | RepoKG requires a Git repository. `dfm diff --structure` prints the reason when a graph cannot be built. |
| `rust-analyzer … does not run` in `dfm doctor` | rustup installed a proxy without the component. Run `rustup component add rust-analyzer`. |
| Swift: `plugin for module 'TestingMacros' not found` | An intermittent toolchain issue. DFM passes the plugin path explicitly; rerun the tests. |
| `check cannot run here` in `dfm benchmark` | The task's toolchain is missing or not on `PATH`. The task is skipped, not failed. |
| A large file appears as a stub | Files over `limits.read_text_bytes` are not read into context. Read a line range, or use `dfm inspect-log`. |
| `dfm resume` reports the session is still running | Its DFM process is still alive. Stop it first. |
| GitHub data marked "from cache, fetched … ago" | GitHub was not allowed or not reachable, so DFM used its cache. |
| Ctrl-C and the next request is slow | The model answers a cancel within five seconds and stays loaded; a worker that does not answer is killed and reloads on the next request. Check `dfm doctor` for memory pressure. |
| `/undo` skipped a file: `its recorded original is missing or does not match its hash` | The copy under `<repo>/.dfm/originals` was changed or removed since DFM recorded it. DFM never writes back an original it cannot verify; restore the file from Git. |
| `dfm doctor` reports disk as `cannot measure` | `$HOME` does not exist or is unreadable; the other checks still run. |

Logs: `~/.local/share/dfm/logs/dfm.jsonl` (JSON lines, redacted). Session traces:
`dfm sessions`, `dfm sessions export <id>`. Per-session metrics: `dfm profile <id>`. Temp
directories of sessions that did not close are removed after a day.
