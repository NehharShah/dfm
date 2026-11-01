# Contributing

DFM runs code and lets a model choose actions, so two rules apply to every change: each behaviour
is covered by a test, and the safety rules in [AGENTS.md](AGENTS.md) do not weaken without an ADR.

Report security issues as described in [SECURITY.md](SECURITY.md), not in public issues.

## Setup

To use DFM, install it from PyPI (`uv tool install "dfm-agent[mlx]"`, see the
[README](README.md)). To work on it, run it from a checkout. macOS on Apple Silicon, Python
3.12+, [uv](https://docs.astral.sh/uv/) and Git are required.

```bash
git clone https://github.com/NehharShah/dfm && cd dfm
uv sync                    # add --extra mlx to run a local model
uv run dfm doctor
```

RepoCanon and RepoKG are released dependencies with version bounds in `pyproject.toml` (PyPI
rejects Git dependencies). A change DFM needs is made in its own repository, released, and the
bound raised here. To develop against local checkouts: `uv pip install -e ../repocanon -e
../repokg`. Some tests use a [reflex-tui](https://github.com/sumervaid/reflex-tui) checkout at
`../reflex-tui` when present.

## Releasing

Only the maintainer publishes. Bump `version` in `pyproject.toml`, move the `[Unreleased]`
section of `CHANGELOG.md` under the new version, commit, then tag `vX.Y.Z` and push the tag.
`.github/workflows/release.yml` builds the sdist and wheel, checks them, smoke-installs the
wheel and publishes `dfm-agent` to PyPI through trusted publishing (no stored token).

## Checks

CI runs these on macOS (Python 3.12, 3.14 and 3.15), and all must pass:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python -m dfm.docsgen --check
```

If you change a config key or a command, run `uv run python -m dfm.docsgen` and commit the
regenerated docs.

| Marker | Requires | Default run |
|---|---|---|
| `threat` | nothing (threat-model regression suite) | runs; must pass |
| `lsp` | a language server | skipped if missing |
| `apple_fm` | Apple FM | skipped if missing |
| `mlx`, `offline_e2e` | `mlx-lm` and `DFM_TEST_MODEL` set to a local model | skipped unless set |
| `slow` | a 1 GB log fixture | excluded; run with `-m slow` |

Live model tests are opt-in because they load the model and heat the machine:
`DFM_TEST_MODEL=mlx-community/Qwen3.8-27B-4bit uv run pytest -m mlx` (a directory also works).

Tests never see your shell `PATH`: the `home` fixture limits it to the test venv and the
system directories, so `uv run pytest` and `.venv/bin/python -m pytest` behave the same and no
test can reach your real `gh`, `kubectl` or `cargo` by accident. A test that deliberately
exercises an installed toolchain adds its directory with `tests.helpers.path_with_tools(...)`,
and builds environments with `tests.helpers.make_env`.

## Pull requests

- Open an issue before starting anything larger than a bug fix.
- One change per PR. Follow the conventions of the surrounding code.
- Include a test that fails without the change. Security fixes add a regression test to
  `tests/test_threats.py` or to the affected module's tests.
- Record significant design decisions as an ADR in [docs/decisions/](docs/decisions), and update
  any documentation the change affects.
- Do not add hosted-model SDKs to runtime dependencies (`tests/test_no_remote_inference.py`).
- Do not commit model weights, session traces, `.dfm/` or real secrets.
- State what you verified and what you could not.

By submitting a contribution, you agree that it is licensed under the project's
[MIT License](LICENSE) and that you have the right to submit it.

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
