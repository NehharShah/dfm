"""Everything one DFM invocation needs, wired once.

    config → execution environment → process manager → policy → session trace
           → models (Apple FM now, MLX coders from 018-020) → router → providers

Commands build a Runtime, do their work through it, and close it with an
honest status. Every model, policy and integration event lands in the session trace.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dfm.config import ConfigOverrides, DFMConfig, LoadedConfig, StateDirs, load_config, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.integrations.reflex import ReflexProvider
from dfm.integrations.repocanon import RepoCanonProvider
from dfm.integrations.repokg import RepoKGProvider
from dfm.models.apple import AppleFMModel
from dfm.models.base import Model
from dfm.models.mlx.discovery import describe_dir
from dfm.models.mlx.model import LocalModelSpec, MLXModel
from dfm.models.pool import ModelPool
from dfm.models.resources import ModelResourceManager
from dfm.models.thermal import limit_level
from dfm.observability import configure_logging, summarize
from dfm.observability import start as start_metrics
from dfm.observability import write as write_metrics
from dfm.observability.metrics import MetricsSink
from dfm.policy import Approver, InteractiveApprover, Policy
from dfm.processes import ProcessManager, ProcessRegistry, ProcessResult
from dfm.routing import RouteDecision, Router
from dfm.sessions import Session, SessionStatus, SessionStore, trace_policy
from dfm.tools.git.hardening import harden_process
from dfm.tools.shell.runner import CommandRunner

_log = logging.getLogger("dfm.session")
EventListener = Callable[[str, dict[str, Any]], None]


def _local_coders(
    config: DFMConfig,
    env: ExecutionEnvironment,
    resources: ModelResourceManager,
    primary: Model | None,
    small: Model | None,
) -> tuple[Model | None, Model | None]:
    """Configured MLX coders (lazy: nothing loads until first use). Same weights → one instance."""
    if primary is None and config.models.primary.model:
        path = Path(config.models.primary.model).expanduser()
        primary = MLXModel(
            LocalModelSpec(
                "primary",
                path,
                path.name,
                config.models.primary.context,
                thermal_limit=limit_level(config.models.thermal_limit),
                seed=config.models.seed,
            ),
            env,
            resources=resources,
        )
    if small is None and config.models.fallback.model:
        path = Path(config.models.fallback.model).expanduser()
        same = isinstance(primary, MLXModel) and primary.spec.path.resolve() == path.resolve()
        if not same:
            small = MLXModel(
                LocalModelSpec(
                    "small",
                    path,
                    path.name,
                    config.models.fallback.context,
                    thermal_limit=limit_level(config.models.thermal_limit),
                    seed=config.models.seed,
                ),
                env,
                resources=resources,
            )
    if primary is None and small is not None:
        primary = None  # coder("primary") falls back to small automatically
    return primary, small


@dataclass
class Runtime:
    repo: Path
    loaded: LoadedConfig
    dirs: StateDirs
    env: ExecutionEnvironment
    pm: ProcessManager
    policy: Policy
    store: SessionStore
    session: Session
    pool: ModelPool
    router: Router
    canon: RepoCanonProvider
    graph: RepoKGProvider
    reflex: ReflexProvider
    metrics: MetricsSink | None = None
    # how this runtime was made: a nested one (worktree) must be made the same way, or
    # `--offline` and the developer's approver would be lost inside it
    overrides: ConfigOverrides | None = None
    approver: Approver | None = None
    # live observers of `event` (the interactive session's display); never part of the trace
    listeners: list[EventListener] = field(default_factory=lambda: [])

    @classmethod
    def create(
        cls,
        repo: Path,
        *,
        request: str,
        mode: str,
        overrides: ConfigOverrides | None = None,
        approver: Approver | None = None,
        apple: Model | None = None,
        small: Model | None = None,
        primary: Model | None = None,
        fm_binary: str | None = None,
    ) -> Runtime:
        harden_process()  # DFM's own git processes ignore repository fsmonitor commands
        repo = repo.resolve()
        loaded = load_config(repo, overrides)
        config = loaded.config
        dirs = state_dirs()
        # Reflex reads <sessions>/*.jsonl; with export disabled traces stay in a subdirectory it never scans.
        export = config.integrations.reflex.session_export
        store = SessionStore(dirs.sessions if export else dirs.sessions / "private")
        session = store.create(repo, request, mode=mode)
        configure_logging(dirs.data / "logs")
        env = ExecutionEnvironment.create(
            repo, config, session_id=session.id, overrides=overrides, dirs=dirs
        )
        pm = ProcessManager(ProcessRegistry(dirs.processes), session_id=session.id)
        policy = Policy(config, approver or InteractiveApprover())  # no TTY → ASK becomes DENY
        trace_policy(policy, store, session)

        def on_recovery(failed: str, fallback: str, reason: str) -> None:
            store.append(session, "recovery", failed=failed, fallback=fallback, reason=reason)

        if apple is None and config.models.apple.enabled:
            binary = fm_binary or shutil.which("fm")
            if binary:
                apple = AppleFMModel(pm, env, binary=binary)
        resources = ModelResourceManager()
        primary, small = _local_coders(config, env, resources, primary, small)
        pool = ModelPool(apple=apple, small=small, primary=primary, on_recovery=on_recovery)

        def on_decision(decision: RouteDecision) -> None:
            store.append(
                session,
                "route_decision",
                route=decision.route.value,
                coder=decision.coder,
                source=decision.source,
                confidence=decision.confidence,
                reasons=list(decision.reasons),
            )

        router = Router(pool.helper(), on_decision=on_decision)
        primary_c = (
            describe_dir(Path(config.models.primary.model).expanduser())
            if config.models.primary.model
            else None
        )
        small_c = (
            describe_dir(Path(config.models.fallback.model).expanduser())
            if config.models.fallback.model
            else None
        )
        if primary_c is not None:
            loaded_primary = primary if isinstance(primary, MLXModel) else None
            router.add_adjuster(
                resources.router_adjuster(
                    primary_c,
                    small_c,
                    config.models.primary.context,
                    primary_loaded=lambda: loaded_primary is not None and loaded_primary.loaded,
                )
            )
        integrations = config.integrations
        canon = RepoCanonProvider(enabled=integrations.repocanon.enabled, cache_root=dirs.cache)
        graph = RepoKGProvider(
            enabled=integrations.repokg.enabled,
            cache_root=dirs.cache,
            github=config.network.github == "allow",
        )

        def trace(event_type: str, **payload: Any) -> None:
            store.append(session, event_type, **payload)

        runner = CommandRunner(env, pm, policy, proposed_by="dfm", on_event=trace)

        async def run_reflex(argv: Sequence[str], cwd: Path) -> ProcessResult | None:
            outcome = await runner.run(argv, cwd=cwd, timeout=60)
            return outcome.result

        reflex = ReflexProvider(
            run_reflex, enabled=integrations.reflex.enabled and integrations.reflex.use_handbook
        )
        return cls(
            repo,
            loaded,
            dirs,
            env,
            pm,
            policy,
            store,
            session,
            pool,
            router,
            canon,
            graph,
            reflex,
            start_metrics(session.id),
            overrides,
            approver,
        )

    def event(self, event_type: str, **payload: Any) -> None:
        self.store.append(self.session, event_type, **payload)
        _log.info(event_type, extra={"session": self.session.id, "payload": payload})
        for listener in self.listeners:
            try:
                listener(event_type, payload)
            except Exception:  # a display bug must never stop the work it displays
                _log.exception("event listener failed on %s", event_type)

    async def close(self, status: SessionStatus, summary: str = "") -> None:
        for model in (self.pool.primary, self.pool.small):
            if isinstance(model, MLXModel):
                await model.unload()
        await self.pm.stop_all()
        self.store.complete(self.session, status, summary)
        self.write_metrics()
        self.env.close()

    def write_metrics(self) -> Path | None:
        """`<data>/metrics/<session>.json`: never fails a command."""
        try:
            record = self.store.load(self.session.id)
            wall_ms = (time.monotonic() - self.metrics.started) * 1000 if self.metrics else 0.0
            summary = summarize(record.header, record.events, self.metrics, wall_ms)
            summary["status"] = record.status
            return write_metrics(self.dirs.data / "metrics", summary, self.metrics)
        except (OSError, ValueError) as exc:
            _log.warning("metrics not written: %s", exc)
            return None
