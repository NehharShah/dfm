# 0004 — RepoCanon owns the repository-operating model

Status: accepted (2026-10-25)

Decision: DFM uses `repocanon.analyzer.analyze_repo` and keeps RepoCanon's
`ProjectModel` as-is. No `DFMRepositoryModel`. When DFM needs a fact RepoCanon
lacks and the fact is reusable (e.g. a language's test command), the change goes
upstream. Fallback when RepoCanon is missing: minimal manifest-based discovery,
clearly marked degraded.
