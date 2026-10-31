# User guide

DFM (`dfm`) is a local coding agent for Apple Silicon Macs. It uses Apple FM (`fm`) for routing
and short text and a local MLX model for code. It does not use hosted models.

Reference: [commands](cli.md) · [configuration](config.md) · [policy](policy.md) ·
[troubleshooting](troubleshooting.md) · [threat model](threat-model.md) ·
[architecture](architecture.md) · [integrations](integrations.md)

## Install

Requirements: macOS on Apple Silicon, Python 3.12+, [uv](https://docs.astral.sh/uv/), Git.
Apple FM requires Apple Intelligence (`fm available` reports its status). The local coder
requires the `mlx` extra and a downloaded model.

```bash
git clone https://github.com/NehharShah/dfm && cd dfm
uv sync --extra mlx       # omit --extra mlx to install without the local coder
uv run dfm --version
uv run dfm doctor         # models, tools, integrations, policy
```

[RepoCanon](https://github.com/NehharShah/repocanon) and [RepoKG](https://github.com/NehharShah/repokg)
are installed with DFM as released versions. [Reflex](https://github.com/sumervaid/reflex-tui) is
optional: install it with `npm install -g reflex-skills`, and DFM reads your past practices from
the `reflex` command when it is on `PATH`.

## Set up the local coder

DFM does not download models. Download one, then run `setup`:

```bash
uv run hf download mlx-community/Qwen3.8-27B-4bit   # about 16 GB; suited to 32 GB+ Macs
uv run dfm setup --dry-run              # show the choice without writing config
uv run dfm setup                        # write ~/.config/dfm/config.toml
uv run dfm models                       # list local models
uv run dfm models eval Qwen3.8-27B      # run a short coding check
```

`setup` reads the Hugging Face cache, estimates memory per model and context size, and selects a
primary and a smaller fallback coder. A 27B model at 4-bit needs about 18.5 GB at a 16k context.
DFM will not load a model that does not fit and reports the memory available
([troubleshooting](troubleshooting.md)).

You are responsible for complying with the license of each model you download.

## Daily use

### Interactive session

Run `dfm` in a repository. Type what you want done, or ask a question; DFM shows each step
as it runs (the file it reads, the edit with its diff, the command, the test result) and ends
each request with what it verified and what it did not.

```text
› fix the failing test
● Baseline tests pytest
  ⎿ 0 passed, 1 failed
● Edit src/calc/ops.py  fix the operator
    -    return a - b
    +    return a + b
  ⎿ 1 passed, 0 failed

✓ completed: fixed add
Changed  src/calc/ops.py
Verified
  tests pass: `pytest` (exit 0)
```

The local model loads on the first request and stays loaded until it has been idle for ten
minutes. Requests that change code run the same loop, policy and sandbox as `dfm implement`;
questions are answered from repository facts and never edit; "review my changes" reviews the
working-tree diff. Earlier requests in the session are context for later ones, and DFM can keep
editing files it changed earlier. Once you edit such a file yourself, it is protected again.

| Command | Does |
|---|---|
| `/diff` | the working-tree diff against HEAD |
| `/undo` | revert the files DFM changed in its latest request (files you changed since are kept) |
| `/plan <task>` | plan without editing |
| `/review` | review the working-tree diff |
| `/ask <question>`, `/do <task>` | force a read-only answer, or the agent loop |
| `/status`, `/clear`, `/help`, `/exit` | session state; forget earlier requests; commands; end |

Ctrl-C stops the current request and keeps the session; Ctrl-D or `/exit` ends it. Confirmations
appear inline, and Ctrl-C at a confirmation answers no. Without a terminal (pipes, CI), `dfm`
lists the commands instead; `dfm chat` starts a session explicitly.

### Single commands

Run commands inside a repository, or pass `--repo PATH`.

| Task | Command |
|---|---|
| Ask about the repository | `dfm ask "how is this project structured?"` |
| Plan without editing | `dfm plan "add retries to the HTTP client"` |
| Make and verify a change | `dfm implement "fix the failing test"` |
| Same, in a worktree (returns a patch) | `dfm implement --worktree "…"` |
| Continue after a crash or Ctrl-C | `dfm resume` |
| Review your diff | `dfm review`, `dfm security-review` |
| Work on GitHub issues and PRs | `dfm issue N`, `dfm implement-issue N`, `dfm review-pr N`, `dfm fix-pr N` |
| Fix a failing CI run | `dfm ci`, or `dfm ci --log run.log` |
| Inspect a large log | `dfm inspect-log app.log --cluster --stacks` |
| Start test services (Postgres, Redis, …) | `dfm services --start` |
| Recent sessions and their cost | `dfm status`, `dfm profile` |
| Benchmark on fixtures | `dfm benchmark --ablation coder --ablation full` |

### `dfm implement`

1. **Analyze.** RepoCanon, RepoKG and Reflex facts, the test command, a baseline test run, and
   any local services the tests need.
2. **Act.** The coder proposes one action at a time. DFM executes it through policy and the
   sandbox and returns the result.
3. **Repair.** Tests rerun after each edit. Repeated identical failures stop the loop.
4. **Verify.** Tests, type check, lint and structural impact. The report lists what ran and
   passed; everything else appears under "Not verified".

DFM does not commit, push or rewrite history. Changes stay in your working tree (or in a patch
with `--worktree`) for you to review and commit.

### Flags

- `--offline`: no network access of any kind.
- `--local-models` (default): no remote inference; other network access follows policy.
- `dfm implement --allow-generated`: edit generated files directly instead of their source.

## Offline behaviour

Offline, DFM can analyze repositories, route with Apple FM, code with the local model, build and
test in the sandbox, start local services, review diffs, inspect logs, use cached GitHub data,
and write traces and metrics.

It cannot install packages, fetch uncached GitHub data, pull images, or reach clusters or chains.
It reports what was blocked and the command that would be needed.

## Fixed safeguards

Regardless of configuration, DFM does not send code or prompts to hosted models; push,
force-push, reset or delete branches on its own; read `~/.ssh`, cloud credentials or keychains;
include secrets in model requests; sign transactions or write to clusters without a typed
confirmation; or let repository files change its policy. Details: [policy](policy.md),
[threat model](threat-model.md).

These safeguards reduce risk but do not eliminate it. Review changes before you commit them.

## Data locations

| Data | Location |
|---|---|
| Configuration | `~/.config/dfm/config.toml` |
| Session traces | `~/.local/share/dfm/sessions/` |
| Metrics | `~/.local/share/dfm/metrics/` |
| Logs (JSON, rotated, redacted) | `~/.local/share/dfm/logs/` |
| Integration caches | `~/.local/share/dfm/cache/` |
| Prompt cache | `~/.cache/dfm/kv/` |
| Per-repository state | `<repo>/.dfm/` (excluded from Git via `.git/info/exclude`) |

DFM stores this data only on your machine. Delete these directories to remove it.
