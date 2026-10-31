# 0007 — Apple FM through the `fm` CLI, on-device only

Status: accepted (2026-10-25)

Decision: start with `fm respond --no-stream -m system [-i ...] [--schema file] [-g]`
as a subprocess (measured ~1 s for a short structured call). `fm serve --socket` is
a later optimization under ProcessManager. The adapter refuses any model other than
`system`. Apple FM is a helper (classify, compress, summarize, draft), never the
only coding engine. Every failure (unavailable, timeout, bad JSON, context limit,
CLI drift) raises a typed error; callers fall back to the deterministic router or the
local coder.
