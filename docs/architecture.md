# Interfaces

The contracts between DFM's components. For the system design, read
[ARCHITECTURE.md](../ARCHITECTURE.md) first.

## Core types

```python
# dfm/models/base.py
class Model(Protocol):
    name: str                      # "apple-fm" | "dfm-primary" | "dfm-small"
    async def healthcheck(self) -> ModelHealth: ...
    async def generate(self, req: GenerateRequest) -> GenerateResult: ...
    def stream(self, req: GenerateRequest) -> AsyncIterator[str]: ...

@dataclass(frozen=True)
class GenerateRequest:
    messages: tuple[Message, ...]  # redacted; built by the context engine
    instructions: str | None
    schema: dict | None            # JSON Schema for structured output
    max_tokens: int | None
    temperature: float | None      # None: the model's recommended sampling
    timeout_s: float
    stop: tuple[str, ...]
    purpose: Purpose               # general | classify | transform | code
    reasoning: Reasoning | None    # low | medium | high (chat-template reasoning effort)
    thinking_budget: int | None    # maximum thinking tokens before an answer is forced
    tools: tuple[dict, ...] | None # native tool definitions; otherwise `schema` is used

@dataclass(frozen=True)
class GenerateResult:
    text: str
    model: str
    latency_ms: int
    parsed: dict | None            # validated against the schema, when one was given
    tokens_in: int | None
    tokens_out: int | None
    tokens_cached: int | None      # part of tokens_in reused from an earlier KV cache
    stop_reason: str | None        # stop | length | loop
    thermal_rest_s: float          # time spent cooling the device during the call
```

The orchestrator sees only `Model`. Provider details stay in `dfm/models/apple/` and
`dfm/models/mlx/`.

```python
# dfm/routing/
class Route(StrEnum):
    TOOLS_ONLY; APPLE; SMALL_CODER; PRIMARY_CODER
    APPLE_THEN_CODER; TOOLS_THEN_CODER; CODER_THEN_APPLE

@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reasons: tuple[str, ...]       # signals that fired
    source: Source                 # deterministic | apple | override | fallback | resources
    confidence: float
    coder: Tier | None             # small | primary, for routes that end in a coder
```

```python
# dfm/policy/
class ActionClass(StrEnum):
    SAFE_READ; SAFE_BUILD; SAFE_LOCAL; LOCAL_STATE_CHANGE; WORKTREE_WRITE
    PACKAGE_NETWORK; REMOTE_READ; REMOTE_WRITE; LOCAL_DESTRUCTIVE
    DESTRUCTIVE; DESTRUCTIVE_REMOTE_WRITE; PRIVILEGED; UNKNOWN

class Decision(StrEnum):  ALLOW; ASK; DENY
class Confirmation(StrEnum):  NONE; NORMAL; HARD   # HARD: the user types the target

@dataclass(frozen=True)
class ProposedAction:
    kind: ActionKind               # shell | fs | network | git | model | process
    action_class: ActionClass
    summary: str                   # the redacted command (one part of a compound command)
    proposed_by: str               # "user" | "agent" | "model:<name>" | "untrusted:<source>"
    target: str | None             # path, host, kube context, chain
    network: NetworkCategory | None
    inside_boundary: bool
    reasons: tuple[str, ...]
    command: str | None            # the whole redacted command line, when this is one part of it

@dataclass(frozen=True)
class PolicyVerdict:
    decision: Decision
    action_class: ActionClass
    reason: str
    confirmation: Confirmation
    network: NetworkCategory | None
    approved_by: Literal["policy", "user", "session"] | None
```

Every tool call is classified (with shell semantics: the classifier sees what `/bin/sh` will
run), then authorized by policy part by part (asking the developer when required, showing the
whole line), then run. Without an interactive terminal, `ASK` resolves to `DENY`.

```python
# dfm/provenance/
@dataclass(frozen=True)
class ContextItem:
    source: Literal["user", "repo", "repocanon", "repokg", "reflex", "git", "lsp",
                    "compiler", "test", "github"]
    path: str | None
    line_range: tuple[int, int] | None
    reason: str
    retrieved_at: datetime
    content_hash: str              # sha256 of the redacted content
    confidence: float
    content: str                   # redacted text
    priority: int                  # 1 (user references) to 10 (broad docs)
```

## Repository intelligence

```python
# dfm/repository/intelligence.py
@dataclass
class RepositoryIntelligence:
    canon: repocanon.models.ProjectModel | DegradedCanon  # RepoCanon's type, unchanged
    graph: dict | None                            # RepoKG kg.json document (repokg_version 1)
    freshness: dict[str, str]                     # per source: what it was computed against
    degraded: list[str]                           # integrations that fell back, and why
```

Reflex (past practices and approved skills) is a `Runtime` provider, not part of this object:
its answers are historical evidence, never repository fact. DFM does not copy upstream results
into its own types. Queries ("test command for package X", "reverse dependencies of module Y")
are functions over the upstream types.

Freshness keys record Git HEAD, a dirty-file fingerprint, RepoCanon's `structural_fingerprint`
and RepoKG's `repo.head` and cache state.

## Execution

`ExecutionEnvironment` holds the working directory, the filesystem boundary (repository, DFM
state directories, approved paths), a scrubbed environment (allowlisted variables, secrets
removed, `core.fsmonitor=false` for every git process), the network mode (`offline`,
`local-models` or policy), resource limits, and the parts of DFM's state that repository code
may not even read (`private_state`: policy, traces, logs, metrics, registry, caches). Processes
start only through `ProcessManager`, which records ownership so DFM stops only what it started.
Stale temp directories left by sessions that did not close are swept after a day.

## Model worker

The MLX worker (`dfm/models/mlx/worker.py`) speaks JSON lines on stdin/stdout. Requests carry
an id; replies and stream chunks carry it back. `{"op": "cancel", "target": id}` is read on a
thread, so a request stops after its current token (`stop_reason: "cancelled"`) while the main
thread is generating; the parent kills the worker only if it does not answer within five
seconds. Persisted KV snapshots carry the model path, a fingerprint of the weight files and the
mlx-lm version, and a hash of their token list; a snapshot that does not match, or on which a
generation fails, is deleted.

## Agent loop

Checkpointed stages (`dfm/recovery/checkpoint.py`): `analysis → plan → edit → test →
(repair)* → verification`. Each checkpoint records the original content of every file touched
so far, so `dfm resume` continues and `/undo` reverts. Repair attempts are bounded and recorded.
A failed patch is re-read, relocated, regenerated and validated; it never falls back to
rewriting the whole file.

## Storage

```text
~/.config/dfm/config.toml                       mode 0600 ($XDG_CONFIG_HOME)
~/.local/share/dfm/{sessions,models,cache,logs,metrics,processes,tmp}/   ($XDG_DATA_HOME)
~/.cache/dfm/kv/        persisted KV cache ($XDG_CACHE_HOME)
<repo>/.dfm/            configuration (allowlist), checkpoints, originals (excluded from Git)
```

Session traces are one JSONL file per session (header, then events), in the format Reflex
reads: [ADR 0006](decisions/0006-reflex-protocol.md),
[schema](protocol/dfm-session-v1.schema.json).

## Degradation

| Failure | Behaviour |
|---|---|
| RepoCanon unavailable | deterministic discovery from manifests |
| RepoKG unavailable | text search and language-adapter defaults |
| Reflex unavailable | no historical practices |
| Apple FM unavailable | deterministic routing; local coder for summaries |
| Primary coder out of memory | reduced context, idle models unloaded, then the small coder |
| GitHub unavailable | cached issue and PR data from `~/.local/share/dfm/cache` |
| No interactive terminal | ask resolves to deny; blocked actions are reported |
