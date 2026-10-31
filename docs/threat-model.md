# Threat model

## Assets

Developer secrets (SSH keys, cloud credentials, tokens, wallet keys, `.env` values, cookies),
uncommitted work, Git history, remote systems (GitHub, clusters, databases, chains), machine
availability (memory, CPU), and DFM's own policy and configuration.

## Trust boundaries

```text
TRUSTED      developer input to DFM; configuration written by the developer
POLICY       dfm/policy and dfm/execution, enforced in code, never by the model
UNTRUSTED    repository files (README, AGENTS.md, CLAUDE.md, comments, fixtures, generated
             code), GitHub issues, PRs and comments, CI logs, tool and compiler output,
             downloaded docs, RepoCanon/RepoKG/Reflex output, model output
```

A model may propose tool calls. Policy decides whether they run.

## Threats and controls

| # | Threat | Control |
|---|---|---|
| T1 | Prompt injection in repository files, issues or CI logs | Untrusted content is wrapped and labelled as data; every tool call is policy-checked regardless of who proposed it; the filesystem boundary blocks `~/.ssh` |
| T2 | Repository instructions (AGENTS.md, README) requesting network or destructive commands | As T1; repository content cannot change policy or configuration |
| T3 | Secrets reaching model context | Redaction of every context item; secret-path denylist; environment scrubbing |
| T4 | Network exfiltration | Classifier maps unknown network use to `other_network` (deny); `--offline` blocks all network |
| T5 | Filesystem escape (`../`, symlinks, absolute paths) | Every path resolved against the boundary before any filesystem operation |
| T6 | Git destruction (reset, clean, force push, branch deletion) | Classified destructive or remote write; never automatic; uncommitted work preserved |
| T7 | Malicious package scripts | Installs ask and show the command; scripts run in the sandbox |
| T8 | Kubernetes mutation | Writes classified remote write or destructive; context shown; typed confirmation or deny |
| T9 | Production database mutation | `DROP`, `TRUNCATE`, or `DELETE` without `WHERE` against non-local hosts classified destructive |
| T10 | Mainnet transactions or wallet signing | RPC classification; mainnet writes need typed confirmation; signing never automatic; keys never in context |
| T11 | Remote inference, including Apple Private Cloud Compute | No hosted SDKs in dependencies; Apple adapter pinned to the on-device model; `remote_inference = deny` |
| T12 | Resource exhaustion | Memory admission, model fallback, timeouts on every process |
| T13 | Poisoned Reflex history | Reflex ranks below policy, tests and the developer; skills require developer approval |
| T14 | Privilege escalation | Privileged actions require a typed confirmation |

## Regression tests

`tests/test_threats.py` (`pytest -m threat`, part of the default run) covers every threat. T1–T3
run a planted repository through `dfm implement` with a scripted model and inspect every model
request. The others test the classifier, policy, sandbox and filesystem boundary directly.

| Threat | Test |
|---|---|
| T1, T2 | `test_t1_t2_injected_text_only_reaches_the_model_as_data` |
| T3 | `test_t3_no_secret_bytes_in_any_model_request` |
| T4 | `test_t4_network_to_unknown_hosts_is_denied`, `test_t4_t7_sandboxed_code_cannot_open_sockets` |
| T5 | `test_t5_filesystem_escapes_are_refused` |
| T6 | `test_t6_git_destruction_never_runs_and_dirty_work_survives` |
| T7 | `test_t7_package_installs_ask_and_no_human_means_no`, `test_t4_t7_sandboxed_code_cannot_open_sockets` |
| T8 | `test_t8_cluster_writes_never_silent` |
| T9 | `test_t9_remote_database_mutation_is_denied` |
| T10 | `test_t10_mainnet_transactions_and_keys` |
| T11 | `test_t11_no_remote_inference_path`, `tests/test_no_remote_inference.py` |
| T12 | `test_t12_runaway_processes_and_oversized_models_are_stopped` |
| T13 | `test_t13_reflex_history_is_evidence_not_authority` |
| T14 | `test_t14_privilege_escalation_needs_a_human` |

## Out of scope

A compromised macOS, a malicious `fm` binary, and actions the developer explicitly approves.

These controls reduce risk; they do not guarantee that every unsafe action is prevented. Report
bypasses as described in [SECURITY.md](../SECURITY.md).
