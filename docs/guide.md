# User guide

DFM (`dfm`) is a local coding agent for Apple Silicon Macs. It uses Apple FM (`fm`) for routing
and short text and a local MLX model for code. It does not use hosted models.

Reference: [commands](cli.md) · [configuration](config.md) · [policy](policy.md) ·
[troubleshooting](troubleshooting.md) · [threat model](threat-model.md) ·
[architecture](architecture.md) · [integrations](integrations.md)

## Install

Requirements: macOS on Apple Silicon, Python 3.12 to 3.15, Git, and
[uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/). Apple FM requires Apple
Intelligence (`fm available` reports its status). The local coder requires `mlx-lm`, which DFM
installs on Python 3.12 to 3.14, and a downloaded model.

DFM is on PyPI as `dfm-agent` (the name `dfm` was taken); the command is `dfm`.

```bash
uv tool install --python 3.14 dfm-agent   # or: pipx install --python python3.14 dfm-agent
dfm --version
dfm doctor                         # models, tools, integrations, policy
```

`mlx-lm` does not install on Python 3.15 yet (its dependency `sentencepiece` has no 3.15
build), so pin 3.14. On 3.15 DFM installs without it, answers questions from repository facts
and cannot edit code; `dfm setup` says so. `dfm-agent[mlx]` still works and installs the same
thing. `uv tool upgrade dfm-agent` upgrades; `uv tool uninstall
dfm-agent` removes the command (your data under `~/.config/dfm` and `~/.local/share/dfm` stays
until you delete it, see [data locations](#data-locations)).

[RepoCanon](https://github.com/NehharShah/repocanon) and [RepoKG](https://github.com/NehharShah/repokg)
are installed with DFM as released versions. [Reflex](https://github.com/sumervaid/reflex-tui) is
optional: install it with `npm install -g reflex-skills`, and DFM reads your past practices from
the `reflex` command when it is on `PATH`.

To work on DFM itself, clone the repository and run it from the checkout
([CONTRIBUTING.md](../CONTRIBUTING.md)); every `dfm …` below is then `uv run dfm …`.

## Set up the local coder

DFM does not download models. Download one into the Hugging Face cache, then run `setup`:

```bash
uvx --from huggingface_hub hf download mlx-community/Qwen3.8-27B-4bit   # about 16 GB; 32 GB+ Macs
dfm setup --dry-run              # show the choice without writing config
dfm setup                        # write ~/.config/dfm/config.toml
dfm models                       # list local models
dfm models eval Qwen3.8-27B      # run a short coding check
```

`setup` reads the Hugging Face cache (and `~/.local/share/dfm/models/`, LM Studio's folders),
estimates memory per model and context size, and selects a primary and a smaller fallback coder.
A 27B model at 4-bit needs about 18.5 GB at a 16k context. DFM will not load a model that does
not fit and reports the memory available ([troubleshooting](troubleshooting.md)).

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

Start it inside a project. DFM refuses to start in your home folder, a folder above it, or `/`,
and warns outside a Git repository.

The local model loads on the first request and stays loaded until it has been idle for ten
minutes. Requests that change code run the same loop, policy and sandbox as `dfm implement`.
Questions, including commands such as `ls` or `git log`, are answered by the model looking at
the repository: it reads files, searches and runs read-only commands, and never edits. Without
a local model, questions are answered from repository facts. "Review my changes" reviews the
working-tree diff. Earlier requests in the session are context for later ones, and DFM can keep
editing files it changed earlier. Once you edit such a file yourself, it is protected again.

| Command | Does |
|---|---|
| `/diff` | the working-tree diff against HEAD |
| `/undo` | revert the files DFM changed in its latest request (files you changed since are kept; a recorded original that no longer matches its hash is never written back) |
| `/plan <task>` | plan without editing |
| `/review` | review the working-tree diff |
| `!<command>` | run a command yourself; policy and the sandbox apply, interactive programs do not work |
| `/ask <question>`, `/do <task>` | force a read-only answer, or the agent loop |
| `/status`, `/clear`, `/help`, `/exit` | session state; forget earlier requests; commands; end |

Ctrl-C stops the current request and keeps the session: a running command is killed, and a
generating model stops after its current token and stays loaded, so the next request does not
wait for it or reload it. `/exit`, `exit`, `quit`, `q` or Ctrl-D ends the session. Anything you
type while a request runs is not printed over its output; it is waiting at the next prompt.
Confirmations appear inline and show the whole command line, not only the part that needs
approval; Ctrl-C at a confirmation answers no. An unexpected error fails the request and the
session goes on. Without a terminal (pipes, CI), `dfm` lists the commands instead; `dfm chat`
starts a session explicitly.

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
3. **Repair.** Tests rerun after each edit. Repeated identical failures stop the loop, as do the
   step limit, the repair budget, the task token budget (`limits.task_output_tokens`) and the
   time limit (`limits.task_minutes`).
4. **Verify.** Tests, type check, lint and structural impact. The report lists what ran and
   passed; everything else appears under "Not verified".

DFM does not commit, push or rewrite history. Changes stay in your working tree (or in a patch
with `--worktree`) for you to review and commit. Services DFM started for the task are stopped
when it ends, however it ends.

### Flags

- `--offline`: no network access of any kind.
- `--local-models` (default): no remote inference; other network access follows policy.
- `dfm implement --allow-generated`: edit generated files directly instead of their source.

### Approvals

Policy decides what runs ([policy](policy.md)). Three things ask more often than people expect,
and each is deliberate:

- A `VAR=value` prefix on a command (`FOO=1 pytest`) asks unless the variable is one that cannot
  change what runs (`CI`, `NO_COLOR`, `LC_*`, `RUST_BACKTRACE`, …). `PATH=`, `GIT_*` and
  `*_OPTIONS` prefixes can make a read run something else.
- Shell builtins that change how later commands resolve (`alias`, `export`, `hash`, `set`), a
  bare `cd`, and quoting the classifier cannot see through (`$'…'`, `{a,b}`) ask.
- A compound command gets network access only if every part of it was approved for a network
  category; `git fetch && sed …` runs offline.

## Offline behaviour

Offline, DFM can analyze repositories, route with Apple FM, code with the local model, build and
test in the sandbox, start local services, review diffs, inspect logs, use cached GitHub data,
and write traces and metrics.

It cannot install packages, fetch uncached GitHub data, pull images, or reach clusters or chains.
It reports what was blocked and the command that would be needed.

## Repository configuration

A repository may ship `.dfm/config.toml`. It is untrusted: it can tighten network rules, turn
integrations and standing worktree consent off, and lower `limits.*`. Every other key, and
anything under `[models]`, is ignored with a warning (`dfm config show` lists where each value
came from). Your own `~/.config/dfm/config.toml` is the only place policy is loosened.

## Fixed safeguards

Regardless of configuration, DFM does not send code or prompts to hosted models; push,
force-push, reset or delete branches on its own; let repository code read `~/.ssh`, cloud
credentials, keychains, shell startup files and histories, other coding agents' state, or
DFM's own policy and traces; include secrets in model requests; sign transactions or write to
clusters without a typed confirmation; or let repository files change its policy. Details:
[policy](policy.md), [threat model](threat-model.md).

These safeguards reduce risk but do not eliminate it. Review changes before you commit them.

## Data locations

| Data | Location |
|---|---|
| Configuration (mode 0600) | `~/.config/dfm/config.toml` (`$XDG_CONFIG_HOME`) |
| Session traces | `~/.local/share/dfm/sessions/` (`$XDG_DATA_HOME`) |
| Metrics | `~/.local/share/dfm/metrics/` |
| Logs (JSON, rotated, redacted, mode 0600) | `~/.local/share/dfm/logs/` |
| Integration caches | `~/.local/share/dfm/cache/` |
| Locally stored model weights | `~/.local/share/dfm/models/` |
| A session's temp dir (removed when it closes; stale ones after a day) | `~/.local/share/dfm/tmp/<session>/` |
| Prompt cache | `~/.cache/dfm/kv/` (`$XDG_CACHE_HOME`) |
| Per-repository state: config, checkpoints, originals for `/undo` | `<repo>/.dfm/` (excluded from Git via `.git/info/exclude`) |

DFM stores this data only on your machine. Delete these directories to remove it.
