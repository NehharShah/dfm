# 0012 — Hardening after the 2026-10-31 code review

Status: accepted (2026-10-31)

Context: a review of every directory under `dfm/` reported sandbox escapes, classifier bypasses
and correctness bugs (62 findings in two passes). Each finding was checked against the code, and
each confirmed one was fixed with a regression test.

Decision:

- **Sandbox** (`dfm/security/sandbox.py`). Repository code cannot write DFM's configuration or
  data directories (except the session temp directory), `.git/config`, `config.worktree` or
  `hooks/`, including in submodules and linked worktrees. A wrapped command gets network access
  only if every part that runs code was approved for a network category.
- **Git.** DFM's own Git processes run with `core.fsmonitor=false`
  (`dfm/tools/git/hardening.py`, set at CLI start and in `Runtime.create`).
- **Shell execution.** Commands with operators or redirects run through `/bin/sh -c`, so
  execution matches the shell semantics used for classification. String commands are
  classified as strings.
- **Classifier.** Read-only tools that can run programs or write files (`rg --pre`, `fd -x`,
  `sed e`, awk pipes, `tar -I`, `sort -o`, `yq -i`, `env -S`, `git -c` with code-running keys,
  database client meta-commands) are not reads. Remote targets are taken from every URL and
  connection form. `$VAR` paths are treated as outside the boundary. Read-only program names keep
  their rule only when resolved from system paths.
- **Untrusted proposals.** Anything proposed from untrusted output (`untrusted:<source>`) always
  asks.
- **Approval prompts** show model-controlled text literally, without markup or control
  characters.
- **Diffs** sent to models keep the keys of secret files and drop their values.

Consequences: compound commands that combine a fetch with running code run offline and must be
split. Pipes no longer require `bash -c`. `git reset <commit>` without `--hard` remains a local
state change, because the working tree and reflog keep the work.

Addendum (2026-10-31, DFM run on its own repository): macOS cannot nest sandboxes, and
`sandbox-exec` cannot run setuid binaries such as `/bin/ps`. DFM detects an existing sandbox
(`sandbox_check`) and does not apply a second one; the outer sandbox's limits still apply.
Process start times come from the kernel (`proc_pidinfo`, in the format of `ps -o lstart=`).
Plan mode runs repository tests only when a read-only sandbox can be applied, and otherwise
reports them as not run. Reflex is asked each question once per session rather than at every
step.
