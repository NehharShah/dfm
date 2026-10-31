# Troubleshooting

Run `dfm doctor` first. It checks models, tools, integrations and policy without network
access.

| Symptom | Cause and fix |
|---|---|
| `not enough memory for a local coder (N GB available after headroom)` | The model does not fit in free memory. Close large apps, or choose a smaller model with `dfm setup`. A 20B model needs about 13 GB plus headroom. |
| `no coding model available (run dfm setup …)` | No local coder is configured. Run `dfm setup`. |
| Apple FM unavailable | `fm available` must report the system model as ready (Apple Intelligence enabled). Without it, DFM routes by rules and uses the local coder for text. |
| `refused by policy: … not approved` in a non-interactive run | Without a terminal, ask means deny. Run interactively, or allow the category in `~/.config/dfm/config.toml` ([policy](policy.md)). |
| `missing python package X: uv add X` | DFM detected a missing dependency and asks before installing. Offline, it only reports it. |
| RepoKG unavailable; structural impact not analyzed | RepoKG requires a Git repository. |
| `rust-analyzer … does not run` in `dfm doctor` | rustup installed a proxy without the component. Run `rustup component add rust-analyzer`. |
| Swift: `plugin for module 'TestingMacros' not found` | An intermittent toolchain issue. DFM passes the plugin path explicitly; rerun the tests. |
| `check cannot run here` in `dfm benchmark` | The task's toolchain is missing or not on `PATH`. The task is skipped, not failed. |
| A large file appears as a stub | Files over `limits.read_text_bytes` are not read into context. Read a line range, or use `dfm inspect-log`. |
| `dfm resume` reports the session is still running | Its DFM process is still alive. Stop it first. |
| GitHub data marked "from cache, fetched … ago" | GitHub was not allowed or not reachable, so DFM used its cache. |

Logs: `~/.local/share/dfm/logs/dfm.jsonl` (JSON lines, redacted). Session traces:
`dfm sessions`, `dfm sessions export <id>`. Per-session metrics: `dfm profile <id>`.
