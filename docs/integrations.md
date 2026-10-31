# Integrations

The interfaces DFM uses, as of RepoCanon 0.2.1, RepoKG 0.4.5 and Reflex CLI 0.5.2. Each is
optional at runtime. When one is missing or fails, DFM reports it and continues with less
context.

## Apple FM (`/usr/bin/fm`)

Commands: `available`, `chat`, `count-tokens`, `license`, `respond`, `schema`, `serve`. The only
model is `system` (on-device). DFM passes `-m system` explicitly and refuses any other model.

- `fm available` prints `System model available` when ready.
- `fm respond [-m system] [-i <instructions>] [--schema <file>] [--no-stream] [-g]
  [--save-transcript f] [--resume f] <prompt>`. The prompt may also be read from stdin. Options:
  `--use-case general|content-tagging`,
  `--guardrails default|permissive-content-transformations`.
- `fm schema object --name N --string a --integer b --double c --boolean d [--array]
  [--optional] [--description t] [--object x --schema json] [--anyOf]` emits JSON Schema with
  `title`, `additionalProperties: false`, `x-order`, `required` and `properties`.
- `fm count-tokens -q [-i instructions] <prompt>` returns a token count.
- `fm serve --socket <path>` serves `/health`, `/v1/models` and `/v1/chat/completions` on a
  Unix socket. Inference remains on-device.

DFM calls `fm respond` as a subprocess; a short structured call takes about 1 s. `fm serve`
under `ProcessManager` is an option if per-call latency matters. `dfm doctor` checks `fm license`
acceptance.

## RepoCanon ([repocanon](https://github.com/NehharShah/repocanon), Python 3.11+, pydantic v2)

- Entry point: `repocanon.analyzer.analyze_repo(repo_path, config=None, *, progress=None) ->
  ProjectModel`.
- Configuration: `repocanon.config.load_config(repo)` reads `.repocanon/config.toml`;
  `project_model_path(repo)` is the CLI's cache location.
- `ProjectModel` (schema_version 2): languages, frameworks, package managers, commands
  (`install`, `build`, `dev`, `test`, `lint`, `format`, `typecheck`, extras), topology, monorepo
  packages with their own commands, key directories, test layout, naming conventions,
  architecture boundaries, preferred libraries, anti-patterns, uncertainty notes, findings,
  `structural_fingerprint`.
- Deterministic; no network.

`RepoCanonProvider` runs `analyze_repo` in a thread, caches by `structural_fingerprint` and
HEAD, and exposes the `ProjectModel` unchanged.

## RepoKG ([repokg](https://github.com/NehharShah/repokg), Python 3.9+, standard library only)

- Graph construction: `repokg.cli.build_graph(repo, out, no_github, pr_limit, exclude=(),
  use_cache=True, stream=None, cache_off_note=..., provider="auto") -> dict`.
  - Progress goes to `stream`; DFM passes a buffer.
  - The scan cache is written under `out`; DFM uses `<repo>/.dfm/repokg/`.
  - `no_github=False` calls the GitHub, GitLab or Bitbucket API; DFM passes `no_github=True`
    unless the `github` network policy allows it.
- Diff: `repokg.diff.build(old, new, detect_renames=True) -> delta`, with `shape_changed`,
  `shape_reasons`, `counts`, `render_text`, `render_markdown`.
- Document (`repokg_version: 1`): repo, languages, modules, edges, edge_stats, exclude,
  branches, prs, github_note, ops, findings, uncertainty.

A public `repokg.api` (`build_graph`, `diff_graph`) is proposed upstream. DFM uses it when
present and otherwise wraps `repokg.cli.build_graph` in `RepoKGProvider`.

## Reflex ([reflex-tui](https://github.com/sumervaid/reflex-tui), `reflex` CLI, optional)

DFM does not import Reflex. It runs the read-only CLI as a subprocess under the same policy as
any command: `reflex handbook --json`, `reflex handbook ask "<question>" --json`, and approved
skills under `~/.reflex/skills/`. Each question is asked once per session.

DFM writes JSONL session traces to `~/.local/share/dfm/sessions/`
([schema](protocol/dfm-session-v1.schema.json)), which Reflex can read.

## Local MLX models

Loaded with `mlx-lm` (the `mlx` extra) from the Hugging Face cache. DFM does not download
models. See the [guide](guide.md).

## Known upstream issues

DFM works around these. None has been fixed upstream yet.

| Project | Issue | DFM workaround | Suggested fix |
|---|---|---|---|
| RepoCanon 0.2.1 | In pnpm, yarn or bun workspaces, member packages report `npm` and `npm run …` although the root uses another manager. | `scoped_commands()` rewrites `npm …` to the root manager and returns the package directory as `cwd`. | Inherit the root lockfile's manager. |
| RepoCanon 0.2.1 | In uv projects the Python test command is `pytest`, not `uv run pytest`. | The Python adapter adds `uv run` when uv is the package manager. | Emit `uv run <tool>` when `uv.lock` exists. |
| RepoCanon 0.2.1 | No `py.typed` marker. | `reportMissingTypeStubs = false` in pyright config. | Ship `repocanon/py.typed`. |
| RepoCanon 0.2.1 | Test commands that appear only in CI workflow steps are not detected. | `dfm ask` reports the gap; CI mode parses workflow steps; the Python adapter falls back to pytest or unittest discovery. | Read CI `run:` steps as a lower-confidence source. |
