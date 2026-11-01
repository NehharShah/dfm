from pathlib import Path

import pytest

from dfm.config import ConfigOverrides, load_config
from dfm.config.model import DFMConfig, NetworkSettings
from dfm.policy import ActionClass, Decision, NetworkCategory, Policy
from dfm.policy.network import (
    NetworkMode,
    NetworkPolicy,
    categorize,
    host_of,
    network_action,
    network_mode,
)

C = NetworkCategory


@pytest.mark.parametrize(
    ("target", "category"),
    [
        ("https://api.openai.com/v1/chat/completions", C.REMOTE_INFERENCE),
        ("api.anthropic.com", C.REMOTE_INFERENCE),
        ("https://generativelanguage.googleapis.com/v1beta/models", C.REMOTE_INFERENCE),
        ("openrouter.ai", C.REMOTE_INFERENCE),
        ("api.groq.com:443", C.REMOTE_INFERENCE),
        ("https://api.together.xyz", C.REMOTE_INFERENCE),
        ("api.fireworks.ai", C.REMOTE_INFERENCE),
        ("https://api-inference.huggingface.co/models/x", C.REMOTE_INFERENCE),
        ("router.huggingface.co", C.REMOTE_INFERENCE),
        ("myco.openai.azure.com", C.REMOTE_INFERENCE),
        ("https://github.com/NehharShah/repokg", C.GITHUB),
        ("api.github.com", C.GITHUB),
        ("raw.githubusercontent.com", C.GITHUB),
        ("git@gitlab.com:group/project.git", C.GIT_REMOTE),
        ("https://pypi.org/simple/", C.PACKAGE_REGISTRIES),
        ("files.pythonhosted.org", C.PACKAGE_REGISTRIES),
        ("registry.npmjs.org", C.PACKAGE_REGISTRIES),
        ("static.crates.io", C.PACKAGE_REGISTRIES),
        ("proxy.golang.org", C.PACKAGE_REGISTRIES),
        ("huggingface.co", C.PACKAGE_REGISTRIES),
        ("ghcr.io", C.CONTAINER_REGISTRIES),
        ("registry-1.docker.io", C.CONTAINER_REGISTRIES),
        ("us-docker.pkg.dev", C.CONTAINER_REGISTRIES),
        ("docs.rs", C.DOCUMENTATION),
        ("requests.readthedocs.io", C.DOCUMENTATION),
        ("https://eth-mainnet.g.alchemy.com/v2/KEY", C.BLOCKCHAIN_RPC_READ),
        ("mainnet.infura.io", C.BLOCKCHAIN_RPC_READ),
        ("evil.example.com", C.OTHER),
        ("api.openai.com.evil.example", C.OTHER),
        ("notgithub.com", C.OTHER),
    ],
)
def test_categorize(target: str, category: NetworkCategory) -> None:
    assert categorize(target) is category


@pytest.mark.parametrize(
    "target",
    ["localhost:5432", "http://127.0.0.1:8545", "[::1]:3000", "app.localhost", "0.0.0.0"],  # noqa: S104
)
def test_loopback_is_not_network(target: str) -> None:
    assert categorize(target) is None


def test_host_of() -> None:
    assert host_of("https://User@API.GitHub.com:443/x") == "api.github.com"
    assert host_of("git@github.com:o/r.git") == "github.com"
    assert host_of("example.com:8080/path") == "example.com"


@pytest.mark.parametrize(
    "target",
    [
        "https://api.github.com@evil.example/x",  # a trusted name in the userinfo
        "api.github.com@evil.example:22/x",
        "http://localhost@evil.example/",
        "file:///etc/passwd",
        "",
    ],
)
def test_confusable_targets_are_other_network(target: str) -> None:
    """The host is what follows the userinfo; a trusted name before `@` buys nothing."""
    assert host_of(target) not in {"api.github.com", "localhost"}
    assert categorize(target) is C.OTHER


def test_remote_inference_denied_even_when_other_network_allowed() -> None:
    config = DFMConfig(network=NetworkSettings(other_network="allow"))
    verdict = NetworkPolicy(config).check_target("https://api.anthropic.com/v1/messages")
    assert verdict.decision is Decision.DENY
    assert verdict.network is C.REMOTE_INFERENCE


def test_default_engineering_categories_ask() -> None:
    policy = NetworkPolicy(DFMConfig())
    assert policy.check_target("pypi.org").decision is Decision.ASK
    assert policy.check_target("api.github.com").decision is Decision.ASK
    assert policy.check_target("evil.example.com").decision is Decision.DENY


def test_offline_denies_every_category(tmp_path: Path) -> None:
    loaded = load_config(None, ConfigOverrides(offline=True), env={"HOME": str(tmp_path)})
    policy = NetworkPolicy(loaded.config)
    for category in NetworkCategory:
        assert policy.check(category, "x").decision is Decision.DENY
    assert policy.check_target("localhost:5432").decision is Decision.ALLOW
    assert network_mode(loaded.config, ConfigOverrides(offline=True)) is NetworkMode.OFFLINE


def test_network_modes() -> None:
    assert network_mode(DFMConfig()) is NetworkMode.LOCAL_MODELS
    configured = DFMConfig(network=NetworkSettings(remote_inference="ask"))
    assert network_mode(configured) is NetworkMode.REMOTE_MODELS_CONFIGURED


async def test_network_action_through_policy() -> None:
    config = DFMConfig(network=NetworkSettings(package_registries="allow"))
    policy = Policy(config)
    allowed = await policy.authorize(
        network_action("https://pypi.org/simple/", "tool:uv", ActionClass.PACKAGE_NETWORK)
    )
    assert allowed.decision is Decision.ALLOW
    denied = await policy.authorize(network_action("https://api.openai.com", "model:x"))
    assert denied.decision is Decision.DENY
    local = network_action("http://127.0.0.1:8545", "tool:cast")
    assert local.action_class is ActionClass.SAFE_LOCAL
