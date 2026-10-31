# 0008 — Git safety

Status: accepted (2026-10-25)

Decision: DFM never automatically pushes, force-pushes, resets --hard, cleans,
deletes branches or rewrites history. Before editing it snapshots the dirty state
(paths + hashes, staged vs unstaged); edits to files that were already dirty need
an explicit plan entry; large tasks prefer an isolated `git worktree`. Unusual
states (merge/rebase/cherry-pick in progress, detached HEAD) stop the task and report.
