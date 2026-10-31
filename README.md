# dfm — Developer Foundation Model

[![CI](https://github.com/NehharShah/dfm/actions/workflows/ci.yml/badge.svg)](https://github.com/NehharShah/dfm/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform: macOS on Apple Silicon](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)

A coding agent that runs entirely on your Mac. DFM edits, builds, tests, repairs and verifies
code using on-device models: Apple FM for routing and short text, and a local MLX model for
code. Repository knowledge comes from [RepoCanon](https://github.com/NehharShah/repocanon),
[RepoKG](https://github.com/NehharShah/repokg) and, optionally,
[Reflex](https://github.com/sumervaid/reflex-tui).

DFM sends no code or prompts to hosted models. Other network access (GitHub, package registries)
is off or requires approval, per [policy](docs/policy.md).

**Status:** early (0.0.1). Commands and configuration may change.

## Requirements

- macOS on Apple Silicon, Python 3.12+, [uv](https://docs.astral.sh/uv/), Git.
- Memory for the local model: the recommended 27B model (4-bit) needs about 18.5 GB at its
  default context, so 32 GB or more is recommended. `dfm setup` picks a model that fits.
- Optional: Apple Intelligence, for Apple FM.

## Quick start

```bash
git clone https://github.com/NehharShah/dfm && cd dfm
uv sync --extra mlx
uv run dfm doctor                          # check this machine
uv run dfm setup                           # choose a local model
uv run dfm implement "fix the failing test"
```

## How it works

- The model proposes one action at a time (read, search, patch, run, test). DFM executes it,
  reruns the tests after every edit, and reports only what a command verified.
- Every command is classified and checked against policy, then runs in a per-command macOS
  sandbox. Destructive, production and mainnet actions require a typed confirmation. DFM does
  not commit, push or rewrite history.
- Repository files, issues and CI logs reach the model as untrusted data, with secrets
  redacted.
- Memory checks, thermal pacing, reasoning budgets and a persistent prompt cache keep the model
  usable alongside other apps.

Modes: `ask`, `plan`, `implement`, `review`, `security-review`, `ci`, `fix-pr`,
`implement-issue`, `benchmark`. Full list: [command reference](docs/cli.md).

## Documentation

[Guide](docs/guide.md) · [Architecture](ARCHITECTURE.md) · [Commands](docs/cli.md) ·
[Configuration](docs/config.md) · [Policy](docs/policy.md) · [Threat model](docs/threat-model.md) ·
[Troubleshooting](docs/troubleshooting.md) · [Design decisions](docs/decisions)

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). Report vulnerabilities
privately as described in [SECURITY.md](SECURITY.md). Changes: [CHANGELOG.md](CHANGELOG.md).

## License and notices

DFM is released under the [MIT License](LICENSE) and is provided "as is", without warranty of
any kind. DFM runs commands and changes files on your machine. Its safeguards reduce risk but do
not eliminate it. Review its changes before you commit them.

Models are third-party software under their own licenses, and you are responsible for complying
with the license of any model you download. RepoCanon, RepoKG and Reflex are separate projects
under their own licenses.

Apple, Apple Intelligence, Apple Silicon and macOS are trademarks of Apple Inc. DFM is an
independent project and is not affiliated with or endorsed by Apple.
