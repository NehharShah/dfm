"""Typed DFM configuration. Every default is documented in docs/config.md."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Rule = Literal["allow", "ask", "deny"]


class _Section(BaseModel):
    # TOML has native types, so strict mode costs nothing and stops "yes" becoming True.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RepoCanonSettings(_Section):
    enabled: bool = True


class RepoKGSettings(_Section):
    enabled: bool = True


class ReflexSettings(_Section):
    enabled: bool = True
    session_export: bool = True
    use_skills: bool = True
    use_handbook: bool = True


class IntegrationsSettings(_Section):
    repocanon: RepoCanonSettings = Field(default_factory=RepoCanonSettings)
    repokg: RepoKGSettings = Field(default_factory=RepoKGSettings)
    reflex: ReflexSettings = Field(default_factory=ReflexSettings)


class AppleModelSettings(_Section):
    enabled: bool = True
    remote: bool = False

    @field_validator("remote")
    @classmethod
    def _on_device_only(cls, value: bool) -> bool:
        if value:
            raise ValueError("Apple FM runs on-device only; Private Cloud Compute is never used")
        return value


class LocalModelSettings(_Section):
    # Only local providers exist. A hosted provider name is a validation error, not a fallback.
    provider: Literal["mlx"] = "mlx"
    model: str = ""
    # target context; the resource manager may lower it. Below 1024 no agent step fits.
    context: int = Field(default=32768, ge=1024)


class ModelsSettings(_Section):
    apple: AppleModelSettings = Field(default_factory=AppleModelSettings)
    primary: LocalModelSettings = Field(default_factory=LocalModelSettings)
    fallback: LocalModelSettings = Field(default_factory=LocalModelSettings)
    # macOS thermal pressure at which local generation slows down to let the laptop cool
    thermal_limit: Literal["off", "moderate", "heavy"] = "moderate"
    # fixed sampling seed for reproducible runs (benchmarks); unset: a fresh one each process
    seed: int | None = Field(default=None, ge=0)
    # how hard a reasoning model thinks per agent step: auto is medium, high right after a
    # failed edit or error (the template default can be the most expensive level)
    reasoning: Literal["auto", "low", "medium", "high"] = "auto"


class NetworkSettings(_Section):
    remote_inference: Rule = "deny"
    github: Rule = "ask"
    git_remote: Rule = "ask"
    package_registries: Rule = "ask"
    documentation: Rule = "ask"
    container_registries: Rule = "ask"
    blockchain_rpc_read: Rule = "ask"
    blockchain_rpc_write: Rule = "deny"
    kubernetes: Rule = "ask"  # cluster reads; writes always ask with a hard confirmation
    other_network: Rule = "deny"


class SecuritySettings(_Section):
    # Redaction, the protected-path denylist and the Git guard are not settings: they are
    # always on (AGENTS.md runtime safety rules). Only what has an off position is here.
    repo_filesystem_boundary: bool = True
    # Writes to kube contexts/namespaces that look like production (prod, production, live)
    allow_production_cluster_writes: bool = False


class GitSettings(_Section):
    prefer_worktree_for_large_tasks: bool = True


class LimitsSettings(_Section):
    # files above this are not read into model context (a stub instead)
    read_text_bytes: int = Field(default=256 * 1024, ge=1024, le=64 * 1024 * 1024)
    read_range_lines: int = Field(default=2000, ge=10, le=100_000)
    # output tokens one task may generate in all, across steps and retries (bounds its energy)
    task_output_tokens: int = Field(default=50_000, ge=1000, le=10_000_000)
    # minutes one task may run before DFM stops it and reports (checked between steps)
    task_minutes: int = Field(default=15, ge=1, le=24 * 60)


class DFMConfig(_Section):
    integrations: IntegrationsSettings = Field(default_factory=IntegrationsSettings)
    models: ModelsSettings = Field(default_factory=ModelsSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    git: GitSettings = Field(default_factory=GitSettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
