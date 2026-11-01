# Security policy

DFM runs repository code and acts on proposals from a local model, which can be manipulated by
the content it reads. A way around its safeguards (policy, command classifier, sandbox, secret
redaction or Git guard) is a vulnerability. The threats DFM is designed against are listed in
[docs/threat-model.md](docs/threat-model.md).

## Reporting

Report vulnerabilities privately through a
[GitHub security advisory](https://github.com/NehharShah/dfm/security/advisories/new). Do not open
a public issue.

Include:

- the command or a minimal repository that reproduces the issue;
- what DFM did and what you expected;
- `dfm --version`, the macOS version and the chip.

We aim to acknowledge reports within 7 days. Confirmed issues are fixed with a regression test
in the threat suite (`pytest -m threat`). With your consent, we credit you in the changelog.
Please allow a reasonable time for a fix before you disclose publicly.

## In scope

- A command that runs without the approval its policy requires, including one the classifier
  reads as safe and the shell then runs as something else (see threat T16).
- Reading protected paths, writing outside allowed paths, or reaching the network from inside
  the sandbox without approval.
- A repository's `.dfm/config.toml` changing a setting it is not allowed to.
- Secrets reaching model context, session traces, logs or approval prompts.
- Repository content, issues, CI logs or tool output changing DFM's policy or configuration.
- DFM overwriting uncommitted work, pushing, or rewriting history without being asked.
- Any request to a hosted model.

## Out of scope

- Actions you approved, or allowed in your own configuration.
- The behaviour of code you asked DFM to run, within the sandbox's limits.
- Weaknesses in macOS or `sandbox-exec` itself, and running DFM without the sandbox.
- Vulnerabilities in RepoCanon, RepoKG or Reflex. Report those to their own projects.

## Supported versions

While DFM is at 0.x, security fixes land on `main` and ship in the next `dfm-agent` release
on PyPI; earlier releases are not patched. Upgrade with `uv tool upgrade dfm-agent`.
