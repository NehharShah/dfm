# 0014 — Distribution: `dfm-agent` on PyPI, released dependencies, tag-driven publishing

Status: accepted (2026-10-31)

Context: DFM was installable only from a checkout (`uv sync`, `uv run dfm …`). The name `dfm`
on PyPI belongs to an unrelated, active project (a dotfile manager). RepoCanon and RepoKG were
pinned to Git commits, which PyPI rejects in package metadata; both are already released on
PyPI under the same maintainer.

Decision:

- **Distribution name `dfm-agent`.** The import package and the console command stay `dfm`.
  `dfm.__version__` reads the `dfm-agent` distribution.
- **Released dependencies with bounds** (`repocanon>=0.2.1,<0.3`, `repokg>=0.4.5,<0.5`), no
  `[tool.uv.sources]`. A change DFM needs in RepoCanon or RepoKG is released there first, then
  the bound is raised here. DFM already tolerates both the current and the previous RepoKG
  module layout (`repokg.api` → `repokg.cli`).
- **The sdist ships only `dfm/`, README, LICENSE, CHANGELOG and SECURITY.** Docs, tests,
  fixtures and working files are not part of the package.
- **Publishing is a tag push** (`vX.Y.Z`, equal to the `pyproject.toml` version) handled by
  `.github/workflows/release.yml`: build, `twine check --strict`, smoke-install the wheel and run
  `dfm --version`, then publish through PyPI trusted publishing (OIDC, GitHub environment
  `pypi`). No API token is stored. CI builds and checks the package on every PR.
- **Recommended install is `uv tool install "dfm-agent[mlx]"`** (or the Git URL before a
  release). Checkouts keep `uv run dfm` for development.

Consequences: contributors and agents still never publish (AGENTS.md); the maintainer creates
the PyPI project's trusted publisher once (owner `NehharShah`, repository `dfm`, workflow
`release.yml`, environment `pypi`). Users who also have the unrelated `dfm` package installed
get a command-name clash; `uv tool` keeps each tool in its own environment, so only the shim
on `PATH` collides.

Addendum (2026-10-31): `mlx-lm` carries an upper bound (`>=0.32.2,<0.33`). It is a 0.x project
whose cache classes and `stream_generate` the worker uses directly, and CI never installs the
extra, so a breaking minor would otherwise surface only on user machines; the bound is raised
after `pytest -m mlx` against the new series. CI's matrix is 3.12, 3.14 and 3.15. The README and
guide lead with `uv tool install "dfm-agent[mlx]"`; the checkout path is for development.

Addendum (2026-11-01, first PyPI install of 0.0.5): `pipx install dfm-agent` (no extra) gave a
DFM that could not edit code, and `dfm setup` printed the fix as `uv tool install 'dfm-agent'`
because Rich read `[mlx]` as a style tag. With the extra, pipx on Python 3.15 failed to build
`sentencepiece`, which mlx-lm needs and which has no 3.15 wheel. Changes:

- **mlx-lm is a dependency**, with the marker `sys_platform == 'darwin' and platform_machine ==
  'arm64' and python_version < '3.15'`. A plain `dfm-agent` install is the working one; on other
  platforms and on 3.15 the install still succeeds, without the local coder. The `mlx` extra
  stays, empty, so `dfm-agent[mlx]` keeps resolving. The 3.15 marker comes off once
  sentencepiece publishes a 3.15 wheel.
- **The install docs pin Python 3.14** (`uv tool install --python 3.14 dfm-agent`), because
  `pipx` and `uv tool` otherwise use the newest Python they find.
- **`dfm setup` and `dfm doctor` say why mlx-lm is missing** (old install, Python 3.15, not Apple
  Silicon) with the command that fixes it, print their text without Rich markup, and list the
  MLX models found instead of "found: none".
- CI's 3.12 and 3.14 legs now install mlx-lm (macOS 15 runners are Apple Silicon); the 3.15 leg
  covers the install without it.
