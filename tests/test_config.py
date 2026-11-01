import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.config import ConfigError, ConfigOverrides, DFMConfig, load_config, state_dirs
from dfm.config.loader import flatten
from dfm.config.paths import discover_repo_root

# The documented defaults, verbatim.
SPEC_DEFAULTS = {
    "integrations": {
        "repocanon": {"enabled": True},
        "repokg": {"enabled": True},
        "reflex": {
            "enabled": True,
            "session_export": True,
            "use_skills": True,
            "use_handbook": True,
        },
    },
    "models": {
        "apple": {"enabled": True, "remote": False},
        "primary": {"provider": "mlx", "model": "", "context": 32768},
        "fallback": {"provider": "mlx", "model": "", "context": 32768},
        "thermal_limit": "moderate",
        "seed": None,
        "reasoning": "auto",
    },
    "network": {
        "remote_inference": "deny",
        "github": "ask",
        "git_remote": "ask",
        "package_registries": "ask",
        "documentation": "ask",
        "container_registries": "ask",
        "blockchain_rpc_read": "ask",
        "blockchain_rpc_write": "deny",
        "kubernetes": "ask",  # cluster writes
        "other_network": "deny",
    },
    "security": {
        "repo_filesystem_boundary": True,
        "allow_production_cluster_writes": False,
    },
    "git": {"prefer_worktree_for_large_tasks": True},
    "limits": {
        "read_text_bytes": 262144,
        "read_range_lines": 2000,
        "task_output_tokens": 50000,
        "task_minutes": 15,
    },
}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home)}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".dfm").mkdir(parents=True)
    return root


def write_user(env: dict[str, str], text: str) -> Path:
    path = Path(env["HOME"]) / ".config" / "dfm" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_repo(repo: Path, text: str) -> None:
    (repo / ".dfm" / "config.toml").write_text(text)


# defaults


def test_missing_config_gives_spec_defaults(env: dict[str, str], repo: Path) -> None:
    loaded = load_config(repo, env=env)
    assert loaded.config.model_dump() == SPEC_DEFAULTS
    assert set(loaded.sources.values()) == {"default"}
    assert loaded.warnings == ()


def test_model_defaults_match_spec() -> None:
    assert DFMConfig().model_dump() == SPEC_DEFAULTS


def test_config_is_immutable() -> None:
    config = DFMConfig()
    with pytest.raises(ValueError):
        config.network.github = "allow"  # type: ignore[misc]


# precedence


def test_user_overrides_default(env: dict[str, str]) -> None:
    write_user(env, '[network]\ngithub = "allow"\n')
    loaded = load_config(None, env=env)
    assert loaded.config.network.github == "allow"
    assert loaded.sources["network.github"] == "user"
    assert loaded.sources["network.git_remote"] == "default"


def test_repo_tightens_user(env: dict[str, str], repo: Path) -> None:
    write_user(env, '[network]\ngithub = "allow"\n')
    write_repo(repo, '[network]\ngithub = "deny"\npackage_registries = "deny"\n')
    loaded = load_config(repo, env=env)
    assert loaded.config.network.github == "deny"
    assert loaded.config.network.package_registries == "deny"
    assert loaded.sources["network.github"] == "repo"


def test_cli_offline_beats_everything(env: dict[str, str], repo: Path) -> None:
    write_user(env, '[network]\ngithub = "allow"\nother_network = "allow"\n')
    loaded = load_config(repo, ConfigOverrides(offline=True), env=env)
    network = loaded.config.network.model_dump()
    assert set(network.values()) == {"deny"}
    assert all(loaded.sources[f"network.{k}"] == "cli" for k in network)


def test_cli_local_models_denies_remote_inference(env: dict[str, str]) -> None:
    write_user(env, '[network]\nremote_inference = "allow"\ngithub = "allow"\n')
    loaded = load_config(None, ConfigOverrides(local_models=True), env=env)
    assert loaded.config.network.remote_inference == "deny"
    assert loaded.sources["network.remote_inference"] == "cli"
    assert loaded.config.network.github == "allow"  # engineering networking follows policy


@pytest.mark.parametrize(
    ("toml", "key", "value"),
    [
        ("[integrations.reflex]\nenabled = false", "integrations.reflex.enabled", False),
        (
            "[integrations.reflex]\nsession_export = false",
            "integrations.reflex.session_export",
            False,
        ),
        ("[integrations.repocanon]\nenabled = false", "integrations.repocanon.enabled", False),
        (
            "[git]\nprefer_worktree_for_large_tasks = false",
            "git.prefer_worktree_for_large_tasks",
            False,
        ),
        ("[limits]\ntask_minutes = 5", "limits.task_minutes", 5),
        ("[limits]\nread_text_bytes = 4096", "limits.read_text_bytes", 4096),
    ],
)
def test_repo_may_turn_off_and_lower(
    env: dict[str, str], repo: Path, toml: str, key: str, value: object
) -> None:
    write_repo(repo, toml + "\n")
    loaded = load_config(repo, env=env)
    assert flatten(loaded.config.model_dump())[key] == value
    assert loaded.sources[key] == "repo"
    assert loaded.warnings == ()


# repo cannot loosen


@pytest.mark.parametrize(
    ("toml", "key"),
    [
        ('[network]\ngithub = "allow"', "network.github"),
        ('[network]\nremote_inference = "allow"', "network.remote_inference"),
        ('[network]\nremote_inference = "ask"', "network.remote_inference"),
        ('[network]\nother_network = "allow"', "network.other_network"),
        ('[network]\nblockchain_rpc_write = "ask"', "network.blockchain_rpc_write"),
        (
            "[security]\nallow_production_cluster_writes = true",
            "security.allow_production_cluster_writes",
        ),
        ('[network]\nkubernetes = "allow"', "network.kubernetes"),
        ("[security]\nrepo_filesystem_boundary = false", "security.repo_filesystem_boundary"),
        # budgets may only go down (T12)
        ("[limits]\ntask_minutes = 1000", "limits.task_minutes"),
        ("[limits]\ntask_output_tokens = 1000000", "limits.task_output_tokens"),
        ("[limits]\nread_text_bytes = 16777216", "limits.read_text_bytes"),
        ("[limits]\nread_range_lines = 50000", "limits.read_range_lines"),
    ],
)
def test_repo_cannot_loosen(env: dict[str, str], repo: Path, toml: str, key: str) -> None:
    write_repo(repo, toml + "\n")
    loaded = load_config(repo, env=env)
    assert flatten(loaded.config.model_dump()) == flatten(SPEC_DEFAULTS)
    assert loaded.sources[key] == "default"
    assert any(key in w and "only tighten" in w for w in loaded.warnings), loaded.warnings


@pytest.mark.parametrize(
    ("user", "repo_toml", "key"),
    [
        # a user who turned something off: the repo cannot turn it back on
        (
            "[integrations.reflex]\nsession_export = false",
            "[integrations.reflex]\nsession_export = true",
            "integrations.reflex.session_export",
        ),
        (
            "[git]\nprefer_worktree_for_large_tasks = false",
            "[git]\nprefer_worktree_for_large_tasks = true",
            "git.prefer_worktree_for_large_tasks",
        ),
        ("[limits]\ntask_minutes = 5", "[limits]\ntask_minutes = 15", "limits.task_minutes"),
    ],
)
def test_repo_cannot_undo_what_the_user_tightened(
    env: dict[str, str], repo: Path, user: str, repo_toml: str, key: str
) -> None:
    write_user(env, user + "\n")
    write_repo(repo, repo_toml + "\n")
    loaded = load_config(repo, env=env)
    assert loaded.sources[key] == "user"
    assert any(key in w and "only tighten" in w for w in loaded.warnings), loaded.warnings


@pytest.mark.parametrize(
    "toml",
    [
        "[models]\nreasoning = 'high'",
        "[models.primary]\ncontext = 4096",
        "[network]\ngopher = 'deny'",  # unknown keys are reported as such
        "[limits]\ntask_minutes = 100000",  # above the model's ceiling: invalid, not a tighten
    ],
)
def test_repo_cannot_set_anything_else(env: dict[str, str], repo: Path, toml: str) -> None:
    """Values out of range are rejected before the tighten check (`limits.task_minutes =
    100000` is invalid, not merely loosening): either way the default stands."""
    write_repo(repo, toml + "\n")
    loaded = load_config(repo, env=env)
    assert loaded.config.model_dump() == SPEC_DEFAULTS
    assert all(v == "default" for v in loaded.sources.values())
    assert loaded.warnings, toml


def test_repo_allowlist_is_the_whole_story(env: dict[str, str], repo: Path) -> None:
    """Every key the model knows is either allowlisted or refused from the repo layer; no key
    falls through untouched (the denylist bug this replaces)."""
    from dfm.config.loader import _repo_may_set  # pyright: ignore[reportPrivateUsage]

    allowed = {k for k in flatten(SPEC_DEFAULTS) if _repo_may_set(k)}
    assert allowed == {
        *(f"network.{k}" for k in SPEC_DEFAULTS["network"]),
        *(f"limits.{k}" for k in SPEC_DEFAULTS["limits"]),
        "security.repo_filesystem_boundary",
        "security.allow_production_cluster_writes",
        "git.prefer_worktree_for_large_tasks",
        "integrations.repocanon.enabled",
        "integrations.repokg.enabled",
        "integrations.reflex.enabled",
        "integrations.reflex.session_export",
        "integrations.reflex.use_skills",
        "integrations.reflex.use_handbook",
    }


def test_repo_cannot_choose_models(env: dict[str, str], repo: Path) -> None:
    write_repo(repo, '[models.primary]\nmodel = "/tmp/evil-weights"\n')
    loaded = load_config(repo, env=env)
    assert loaded.config.models.primary.model == ""
    assert any("cannot choose models" in w for w in loaded.warnings)


def test_repo_invalid_value_is_skipped_not_fatal(env: dict[str, str], repo: Path) -> None:
    write_repo(repo, '[network]\ngithub = 5\ndocumentation = "deny"\n')
    loaded = load_config(repo, env=env)
    assert loaded.config.network.github == "ask"
    assert loaded.config.network.documentation == "deny"
    assert any("invalid value" in w for w in loaded.warnings)


def test_repo_malformed_toml_is_ignored(env: dict[str, str], repo: Path) -> None:
    write_repo(repo, "[network\n")
    loaded = load_config(repo, env=env)
    assert loaded.config.model_dump() == SPEC_DEFAULTS
    assert any("cannot be read" in w for w in loaded.warnings)


# user-level validation


def test_apple_remote_rejected(env: dict[str, str]) -> None:
    write_user(env, "[models.apple]\nremote = true\n")
    with pytest.raises(ConfigError, match="on-device only"):
        load_config(None, env=env)


def test_hosted_provider_rejected(env: dict[str, str]) -> None:
    write_user(env, '[models.primary]\nprovider = "openai"\n')
    with pytest.raises(ConfigError, match=r"models\.primary\.provider"):
        load_config(None, env=env)


def test_strict_types(env: dict[str, str]) -> None:
    write_user(env, '[integrations.reflex]\nenabled = "no"\n')
    with pytest.raises(ConfigError):
        load_config(None, env=env)


def test_user_malformed_toml_is_an_error(env: dict[str, str]) -> None:
    write_user(env, "[network\n")
    with pytest.raises(ConfigError, match="cannot be read"):
        load_config(None, env=env)


def test_user_remote_inference_warns(env: dict[str, str]) -> None:
    write_user(env, '[network]\nremote_inference = "ask"\n')
    loaded = load_config(None, env=env)
    assert loaded.config.network.remote_inference == "ask"
    assert any("remote_inference" in w for w in loaded.warnings)


def test_unknown_keys_warn(env: dict[str, str]) -> None:
    write_user(env, '[network]\ngopher = "allow"\n[flux]\nx = 1\n')
    loaded = load_config(None, env=env)
    assert loaded.config.model_dump() == SPEC_DEFAULTS
    assert any("network.gopher" in w for w in loaded.warnings)
    assert any("'flux'" in w for w in loaded.warnings)


# XDG paths


def test_default_paths(env: dict[str, str]) -> None:
    home = Path(env["HOME"])
    dirs = state_dirs(env)
    assert dirs.config == home / ".config" / "dfm"
    assert dirs.data == home / ".local" / "share" / "dfm"
    assert dirs.sessions == dirs.data / "sessions"
    assert load_config(None, env=env).user_path == home / ".config" / "dfm" / "config.toml"


def test_xdg_overrides(env: dict[str, str], tmp_path: Path) -> None:
    env = {**env, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "dat")}
    dirs = state_dirs(env)
    assert dirs.config == tmp_path / "cfg" / "dfm"
    assert dirs.processes == tmp_path / "dat" / "dfm" / "processes"


def test_relative_xdg_ignored(env: dict[str, str]) -> None:
    dirs = state_dirs({**env, "XDG_CONFIG_HOME": "relative/cfg"})
    assert dirs.config == Path(env["HOME"]) / ".config" / "dfm"


def test_state_dirs_ensure_private(env: dict[str, str]) -> None:
    dirs = state_dirs(env)
    assert not dirs.data.exists()
    dirs.ensure()
    for path in (dirs.sessions, dirs.models, dirs.cache, dirs.logs, dirs.metrics, dirs.processes):
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_load_does_not_create_directories(env: dict[str, str]) -> None:
    load_config(None, env=env)
    assert list(Path(env["HOME"]).iterdir()) == []


def test_discover_repo_root(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / ".git").mkdir(parents=True)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert discover_repo_root(nested) == root.resolve()
    assert discover_repo_root(tmp_path) == tmp_path.resolve()


# CLI

runner = CliRunner()


def test_cli_show_json(env: dict[str, str], repo: Path) -> None:
    write_repo(repo, '[network]\ngithub = "allow"\n')
    result = runner.invoke(app, ["config", "show", "--json", "--repo", str(repo)], env=env)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["config"] == SPEC_DEFAULTS
    assert payload["sources"]["network.github"] == "default"
    assert any("only tighten" in w for w in payload["warnings"])


def test_cli_offline_flag(env: dict[str, str], repo: Path) -> None:
    result = runner.invoke(
        app, ["--offline", "config", "show", "--json", "--repo", str(repo)], env=env
    )
    assert result.exit_code == 0, result.output
    assert set(json.loads(result.stdout)["config"]["network"].values()) == {"deny"}


def test_cli_show_table(env: dict[str, str], repo: Path) -> None:
    result = runner.invoke(app, ["config", "show", "--repo", str(repo)], env=env)
    assert result.exit_code == 0, result.output
    assert "network.remote_inference" in result.stdout
    assert '"deny"' in result.stdout


def test_cli_invalid_user_config_exits_2(env: dict[str, str], repo: Path) -> None:
    write_user(env, "[models.apple]\nremote = true\n")
    result = runner.invoke(app, ["config", "show", "--repo", str(repo)], env=env)
    assert result.exit_code == 2


def test_cli_path(env: dict[str, str], repo: Path) -> None:
    result = runner.invoke(app, ["config", "path", "--repo", str(repo)], env=env)
    assert result.exit_code == 0, result.output
    assert str(Path(env["HOME"]) / ".config" / "dfm" / "config.toml") in result.stdout
    assert "not found" in result.stdout


def test_an_invalid_value_leaves_the_config_untouched(tmp_path: Path) -> None:
    from dfm.config import ConfigError
    from dfm.config.writer import set_user_value

    env = {"HOME": str(tmp_path)}
    path = set_user_value("network.github", "allow", env)
    before = path.read_text()
    with pytest.raises(ConfigError):
        set_user_value("network.github", "sometimes", env)
    assert path.read_text() == before
    assert load_config(None, env=env).config.network.github == "allow"  # still loads
