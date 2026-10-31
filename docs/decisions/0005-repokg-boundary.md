# 0005 — RepoKG owns the structural graph

Status: accepted (2026-10-25)

Decision: DFM uses RepoKG's kg.json document and `diff.build` delta directly. No
`DFMCodeGraph`. RepoKG's graph builder lives in `repokg.cli`; a stable `repokg.api`
(`build_graph`, `diff_graph`) is proposed upstream and used when present. DFM disables GitHub
lookup unless network policy permits it, captures RepoKG's progress output, and keeps its cache
in `<repo>/.dfm/repokg/` so it never writes to tracked files.
