# Policy reference

Every action (shell command, file write, network use, model call) is classified and then
decided by policy: **allow**, **ask** (you confirm) or **deny**. The same rules apply whether
you, DFM or the model proposed the action. Without an interactive terminal, ask means deny. A
repository's own configuration (`.dfm/config.toml`) is an allowlist: it can tighten network
rules, turn integrations and standing worktree consent off, and lower limits; anything else it
sets is ignored with a warning.

## Action classes

| Class | Examples | Default |
|---|---|---|
| SAFE_READ | `ls`, `cat` inside the repo, `git status`, `kubectl config current-context` | allow |
| SAFE_LOCAL | offline helpers, `helm template`, transactions on a local chain, cleanup of DFM-labelled containers | allow |
| SAFE_BUILD | tests, builds, linters (`pytest`, `cargo test`, `go vet`, `tsc`), sandboxed | allow |
| WORKTREE_WRITE | edits inside the repo, formatters, codegen (`protoc`, `buf generate`) | allow |
| PACKAGE_NETWORK | `uv add`, `npm install`, `cargo fetch`, `go get` | `network.package_registries` (ask) |
| REMOTE_READ | `gh pr view`, `git fetch`, `kubectl get`, `cast call` | per network category |
| LOCAL_STATE_CHANGE | starting services, `docker compose up`, `brew services start` | ask |
| REMOTE_WRITE | `gh pr comment`, `git push`, `kubectl apply`, `cast send` | ask |
| LOCAL_DESTRUCTIVE | `docker rm`, `docker compose down` of another project | ask |
| DESTRUCTIVE | `git reset --hard`, `git clean -fd`, `docker system prune` | typed confirmation |
| DESTRUCTIVE_REMOTE_WRITE | `git push --force`, `kubectl delete`, remote `DROP TABLE` | typed confirmation |
| PRIVILEGED | `sudo`, key material on the command line, privileged containers, `kubectl config view --raw` | typed confirmation |
| UNKNOWN | anything without a rule (`python -c …`, binaries run by path), and anything the shell would reinterpret after classification: `alias`/`export`/`hash`/`set`, a `VAR=value` prefix outside a short benign list, `$'…'` or `{a,b}` quoting, a bare `cd`, `sed`/`awk` scripts that can run programs | ask |

Commands are classified the way `/bin/sh` will run them: pipes, redirects, `&&`, newlines,
wrappers (`env`, `timeout`, `xargs`, `sh -c`, `uv run`), abbreviated git long options
(`--har` is `--hard`) and SQL with comments stripped. A compound command is as dangerous as its
most dangerous part, and the confirmation shows the whole line, not only the part that asked.

A **typed confirmation** requires entering the target (kube context, chain, path), not "y".
Session-wide approval is available only for normal confirmations and does not carry over to
another session; `dfm resume` starts with no approvals.

## Network categories

Model networking and engineering networking are configured separately. DFM includes no remote
model provider, so `remote_inference` cannot be enabled in practice.

| Key (`[network]`) | Covers | Default |
|---|---|---|
| `remote_inference` | hosted models | deny |
| `github` | `gh` reads (writes always ask) | ask |
| `git_remote` | `git fetch`, `git pull` | ask |
| `package_registries` | installs and fetches | ask |
| `documentation` | documentation lookups | ask |
| `container_registries` | `docker pull`, `docker push` | ask |
| `blockchain_rpc_read` | `cast call`, chain queries | ask |
| `blockchain_rpc_write` | transactions (testnet: ask; mainnet or unknown: typed confirmation) | deny |
| `kubernetes` | cluster reads (writes: typed confirmation; production contexts denied) | ask |
| `other_network` | everything else (`curl`, `wget`, …) | deny |

`--offline` denies every category for the run.

## Special rules

- **Clusters.** The active context and namespace are shown before every `kubectl` or `helm`
  command. Writes require a typed confirmation of the context. Unknown contexts and contexts
  that look like production (`prod`, `production`, `prd`, `live`) are denied unless
  `security.allow_production_cluster_writes` is set in your user configuration. A repository
  cannot set it.
- **Chains.** Local chains (Anvil, devnets) are allowed. Testnet writes ask. Mainnet and
  unrecognised endpoints require a typed confirmation of the chain. Private keys or mnemonics
  proposed by a model are denied and always redacted from traces.
- **Docker.** DFM labels what it creates (`dfm.session`) and namespaces Compose projects
  (`dfm-<session>`). Cleaning up those is allowed; anything broader is destructive.
- **Git.** DFM does not commit, push, reset, rewrite history or delete branches on its own.
  Uncommitted work is protected. Large tasks can run in a worktree. Every git process DFM
  starts, its own or a model's, runs with `core.fsmonitor=false`, so a repository's Git
  configuration cannot execute code when status is read.
- **CI fixes.** Edits that skip or xfail tests, or that stop a workflow step from failing, are
  refused.
- **Generated files.** Changed through their source and the generator, unless you pass
  `--allow-generated`.

## Sandbox and boundaries

Repository code (tests, builds, anything a model proposes) runs under macOS `sandbox-exec`.
Writes are limited to the repository, DFM's temp directory and toolchain caches, and never
reach `<repo>/.dfm` (checkpoint originals, repository policy), the persisted prompt cache,
DFM's own interpreter, Git hooks or Git config. Reads of `~/.ssh`, cloud credentials,
keychains, browser profiles, shell startup files and histories, other coding agents' state,
and DFM's own policy, traces, logs and caches are denied. Network is denied unless every part
of the command was allowed for a network category; read-only tools that can start programs
(`sed`, `awk`, `less`, `find`, `git`) never ride on another part's approval. Filesystem tools
resolve symlinks and refuse paths outside the repository. Child processes receive an
environment without tokens, keys, passwords or credentialed URLs.

Model input is redacted. Repository content, issue text, CI logs and tool output are passed as
untrusted data. See the [threat model](threat-model.md) and its regression suite
(`pytest -m threat`).
