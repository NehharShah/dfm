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
