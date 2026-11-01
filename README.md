# dfm — Developer Foundation Model

[![CI](https://github.com/NehharShah/dfm/actions/workflows/ci.yml/badge.svg)](https://github.com/NehharShah/dfm/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dfm-agent.svg)](https://pypi.org/project/dfm-agent/)
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

**Status:** pre-alpha. Commands and configuration may change between releases.

## Requirements

- macOS on Apple Silicon, Python 3.12 to 3.15, Git, and [uv](https://docs.astral.sh/uv/) or
  [pipx](https://pipx.pypa.io/) to install the command.
- Memory for the local model: the recommended 27B model (4-bit) needs about 18.5 GB at its
  default context, so 32 GB or more is recommended. `dfm setup` picks a model that fits.
- Optional: Apple Intelligence, for Apple FM.

## Install

DFM is published on PyPI as [`dfm-agent`](https://pypi.org/project/dfm-agent/); the command and
the import package are `dfm`.

```bash
uv tool install --python 3.14 dfm-agent   # or: pipx install --python python3.14 dfm-agent
dfm doctor                                 # check this machine: models, tools, policy
dfm setup                                  # choose a local model that fits in memory
cd your-project && dfm                     # interactive session in that repository
```

This installs `mlx-lm`, which runs the local coding model. It does not install on Python 3.15
yet (its dependency `sentencepiece` has no 3.15 build), hence `--python 3.14`; on 3.15 DFM
installs without it, answers questions from repository facts, and cannot edit code. DFM does
not download model weights;
`dfm setup` picks from models already in your Hugging Face cache (the [guide](docs/guide.md)
shows how to fetch one). Upgrade with `uv tool upgrade dfm-agent`.

To work on DFM itself, clone the repository and use `uv sync` and `uv run dfm …`
([CONTRIBUTING.md](CONTRIBUTING.md)).

`dfm` in a repository starts an interactive session: type what you want done, watch each step
run, and `/undo` the last request if you don't want it. Single commands such as
`dfm implement "fix the failing test"` do one task and exit.

## How it works

- The model proposes one action at a time (read, search, patch, run, test). DFM executes it,
  reruns the tests after every edit, and reports only what a command verified.
- Every command is classified with shell semantics and checked against policy before it runs;
  anything the classifier cannot vouch for (`python -c`, an unfamiliar tool, a `PATH=` prefix,
  shell quoting it cannot see through) asks you first. Repository code runs in a per-command
  macOS sandbox that cannot read your keys, shell history or DFM's own state. Destructive,
  production and mainnet actions require a typed confirmation. DFM does not commit, push or
  rewrite history.
- Repository files, issues and CI logs reach the model as untrusted data, with secrets
  redacted. A repository's own `.dfm/config.toml` can tighten DFM's policy, never loosen it.
- Memory checks, thermal pacing, reasoning budgets and a persistent prompt cache keep the model
  usable alongside other apps. Ctrl-C stops the model within a token and keeps it loaded.

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
