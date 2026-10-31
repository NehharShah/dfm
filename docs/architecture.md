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

@dataclass
class RouteDecision:
    route: Route
    reasons: list[str]             # signals that fired
    source: Literal["deterministic", "apple", "reflex", "override", "fallback", "resources"]
    confidence: float
```

```python
# dfm/policy/
class ActionClass(StrEnum):
    SAFE_READ; SAFE_BUILD; SAFE_LOCAL; LOCAL_STATE_CHANGE; WORKTREE_WRITE
    PACKAGE_NETWORK; REMOTE_READ; REMOTE_WRITE; LOCAL_DESTRUCTIVE
    DESTRUCTIVE; DESTRUCTIVE_REMOTE_WRITE; PRIVILEGED; UNKNOWN

class Decision(StrEnum):  ALLOW; ASK; DENY

@dataclass
class PolicyVerdict:
    decision: Decision
    action_class: ActionClass
    reason: str
    network_category: NetworkCategory | None
```

Every tool call is classified, then authorized by policy (asking the developer when required),
then run. Without an interactive terminal, `ASK` resolves to `DENY`.

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
    canon: repocanon.models.ProjectModel | None   # RepoCanon's type, unchanged
    graph: dict | None                            # RepoKG kg.json document (repokg_version 1)
    reflex: ReflexMemory | None                   # practices, skills, corrections
    freshness: Freshness
    degraded: list[str]                           # integrations that fell back, and why
```

DFM does not copy these into its own types. Queries ("test command for package X", "reverse
dependencies of module Y") are functions over the upstream types.

`Freshness` records Git HEAD, a dirty-file fingerprint, RepoCanon's `structural_fingerprint`,
RepoKG's `repo.head` and cache state, and the Reflex scan time.

## Execution

`ExecutionEnvironment` holds the working directory, the filesystem boundary (repository, DFM
state directories, approved paths), a scrubbed environment, the network mode (`offline`,
`local-models` or policy) and resource limits. Processes start only through `ProcessManager`,
which records ownership so DFM stops only what it started.

## Agent loop

Checkpointed stages: `analysis → plan → edit → format → check → test → (parse → repair)* →
broader tests → lint → diff → structural diff → boundary check → verify requirements → trace →
summarize`. Repair attempts are bounded and recorded. A failed patch is re-read, relocated,
regenerated and validated; it never falls back to rewriting the whole file.

## Storage

```text
~/.config/dfm/config.toml
~/.local/share/dfm/{sessions,models,cache,logs,metrics,processes}/
~/.cache/dfm/kv/        persisted KV cache
<repo>/.dfm/            configuration and checkpoints (excluded from Git)
```

Session traces are one JSONL file per session (header, then events), in the format Reflex
reads: [ADR 0006](decisions/0006-reflex-protocol.md),
[schema](protocol/dfm-session-v1.schema.json).

## Degradation

| Failure | Behaviour |
|---|---|
| RepoCanon unavailable | deterministic discovery from manifests |
| RepoKG unavailable | LSP and text search |
| Reflex unavailable | no historical practices |
| Apple FM unavailable | deterministic routing; local coder for summaries |
| Primary coder out of memory | reduced context, idle models unloaded, then the small coder |
| GitHub unavailable | cached issue and PR data from `~/.local/share/dfm/cache` |
| No interactive terminal | ask resolves to deny; blocked actions are reported |
