# Contributing

DFM runs code and lets a model choose actions, so two rules apply to every change: each behaviour
is covered by a test, and the safety rules in [AGENTS.md](AGENTS.md) do not weaken without an ADR.

Report security issues as described in [SECURITY.md](SECURITY.md), not in public issues.

## Setup

macOS on Apple Silicon, Python 3.12+, [uv](https://docs.astral.sh/uv/) and Git are required.

```bash
git clone https://github.com/NehharShah/dfm && cd dfm
uv sync                    # add --extra mlx to run a local model
uv run dfm doctor
```

RepoCanon and RepoKG are pinned Git dependencies (`[tool.uv.sources]` in `pyproject.toml`). To
develop against local checkouts: `uv pip install -e ../repocanon -e ../repokg`. Some tests use
a [reflex-tui](https://github.com/sumervaid/reflex-tui) checkout at `../reflex-tui` when present.

## Checks

CI runs these on macOS, and all must pass:

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
