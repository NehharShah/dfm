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
- **Git.** Every git process DFM starts runs with `core.fsmonitor=false`
  (`dfm/execution/git_hardening.py`, applied by `ExecutionEnvironment.child_env`, so DFM's own
  git and model-proposed git get it alike; `dfm/tools/git/hardening.py` re-exports it).
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

Addendum (2026-10-31, second review, threat T16): the classifier trusted several things the
shell reinterprets after classification. Builtins that change command resolution (`alias`,
`hash`, `export`, `set`, `unset`) and a bare `cd` are `UNKNOWN`; `VAR=value` prefixes are kept
on the part, boundary-checked and `UNKNOWN` unless the variable is on a short benign list
(`PATH=`, `GIT_*`, `*_CONFIG_PATH` can make a read run something else); ANSI-C and brace
quoting floor a command to `UNKNOWN`; `sed` reads its script wherever the options put it and
checks `e`/`w` before `-i`; `awk` matches across lines; git long-option abbreviations are
expanded before matching; SQL comments are stripped before the `WHERE` check; `git rm -r` is
`LOCAL_DESTRUCTIVE`. The approval prompt shows the whole command line under the part that asked.
The git-config environment hardening above was not reaching children (the allowlist dropped
`GIT_CONFIG_*`); it is now injected in `child_env`.
