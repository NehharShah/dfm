# 0001 — Python 3.12+, uv, Typer

Status: accepted (2026-10-25)

Context: MLX, RepoCanon and RepoKG are Python. Reflex is TypeScript and stays that way.

Decision: DFM is Python ≥3.12, managed with uv (`pyproject.toml`, hatchling build),
CLI on Typer + Rich (same as RepoCanon), pydantic v2 for config/schemas,
pytest + ruff + pyright (strict on `dfm/`). Reflex is reached through its CLI
(`--json`), never imported.

Consequences: one language for orchestration. Reflex changes are made in
[reflex-tui](https://github.com/sumervaid/reflex-tui).
