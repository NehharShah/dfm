## What and why

<!-- The change, and the issue or problem it addresses. -->

## Verification

- [ ] `uv run pytest`
- [ ] `uv run ruff check .` and `uv run ruff format --check .`
- [ ] `uv run pyright`
- [ ] `uv run python -m dfm.docsgen --check` (ran `uv run python -m dfm.docsgen` if a config key or command changed)
- [ ] A test that fails without this change

Not verified (for example, no local model or language server):

## Safety

- [ ] Does not weaken policy, the classifier, the sandbox, redaction or the Git guard, or it comes with an ADR and a threat-model update
- [ ] No hosted-model dependency, real secrets, model weights or session traces
- [ ] Docs, ADRs and `CHANGELOG.md` updated where behaviour changed
