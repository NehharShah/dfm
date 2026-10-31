"""Network categories by host, and the policy check for them (ADR 0003).

Model networking and engineering networking are separate: a host that serves
hosted inference is always `remote_inference`, whatever `other_network` says.
Loopback is not network: local services and dev servers stay reachable offline.
"""

import ipaddress
from enum import StrEnum
from urllib.parse import urlsplit

from dfm.config.loader import ConfigOverrides
from dfm.config.model import DFMConfig
from dfm.policy.types import (
    ActionClass,
    Confirmation,
    Decision,
    NetworkCategory,
    PolicyVerdict,
    ProposedAction,
)

C = NetworkCategory

# Exact hosts, or ".suffix" entries matching any subdomain.
_HOSTS: dict[NetworkCategory, tuple[str, ...]] = {
    C.REMOTE_INFERENCE: (
        "api.openai.com",
        ".openai.azure.com",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
        "aiplatform.googleapis.com",
        ".aiplatform.googleapis.com",
        "openrouter.ai",
        "api.groq.com",
        "api.together.xyz",
        "api.together.ai",
        "api.fireworks.ai",
        "api-inference.huggingface.co",
        "router.huggingface.co",
        ".endpoints.huggingface.cloud",
        "api.mistral.ai",
        "api.cohere.ai",
        "api.cohere.com",
        "api.deepseek.com",
        "api.x.ai",
        "api.perplexity.ai",
        "api.replicate.com",
    ),
    C.GITHUB: (
        "github.com",
        "api.github.com",
        "uploads.github.com",
        "codeload.github.com",
        ".githubusercontent.com",
    ),
    C.GIT_REMOTE: ("gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht"),
    C.PACKAGE_REGISTRIES: (
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "registry.yarnpkg.com",
        "crates.io",
        "static.crates.io",
        "index.crates.io",
        "proxy.golang.org",
        "sum.golang.org",
        "rubygems.org",
        "repo.maven.apache.org",
        "repo1.maven.org",
        "scarbs.xyz",
        # Local model weights are packages too; only `dfm setup` / `dfm models pull` fetch them.
        "huggingface.co",
        "cdn-lfs.huggingface.co",
        ".hf.co",
    ),
    C.CONTAINER_REGISTRIES: (
        "docker.io",
        "registry-1.docker.io",
        "auth.docker.io",
        "production.cloudflare.docker.com",
        "ghcr.io",
        "quay.io",
        "gcr.io",
        ".pkg.dev",
        "public.ecr.aws",
        "mcr.microsoft.com",
    ),
    C.DOCUMENTATION: (
        "docs.python.org",
        "docs.rs",
        "doc.rust-lang.org",
        "pkg.go.dev",
        "developer.mozilla.org",
        "developer.apple.com",
        "nodejs.org",
        ".readthedocs.io",
        "docs.soliditylang.org",
        "book.getfoundry.sh",
        "docs.starknet.io",
    ),
    C.BLOCKCHAIN_RPC_READ: (
        ".infura.io",
        ".alchemy.com",
        ".quiknode.pro",
        "rpc.ankr.com",
        "cloudflare-eth.com",
        ".publicnode.com",
        ".blastapi.io",
        "mainnet.base.org",
        "sepolia.base.org",
        ".drpc.org",
    ),
}


class NetworkMode(StrEnum):
    OFFLINE = "offline"  # no network for any purpose
    LOCAL_MODELS = "local-models"  # no remote inference; engineering follows policy (default)
    REMOTE_MODELS_CONFIGURED = "remote-models-configured"  # user config enabled it explicitly


def network_mode(config: DFMConfig, overrides: ConfigOverrides | None = None) -> NetworkMode:
    if overrides is not None and overrides.offline:
        return NetworkMode.OFFLINE
    if config.network.remote_inference != "deny":
        return NetworkMode.REMOTE_MODELS_CONFIGURED
    return NetworkMode.LOCAL_MODELS


def host_of(target: str) -> str:
    """Host from a URL, `host:port`, or `user@host:path` (scp-style git remote)."""
    target = target.strip()
    if "://" in target:
        host = urlsplit(target).hostname or ""
    elif "@" in target and ":" in target.split("@", 1)[1]:
        host = target.split("@", 1)[1].split(":", 1)[0]
    else:
        host = target.split("/", 1)[0]
        if host.startswith("[") and "]" in host:
            host = host[1 : host.index("]")]
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
    return host.lower().rstrip(".")


def is_loopback(host: str) -> bool:
    # An empty host (unparsable URL, `--rpc-url=`, file://) is unknown, never local.
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def categorize(target: str) -> NetworkCategory | None:
    """Category for a host or URL; None means loopback (not network)."""
    host = host_of(target)
    if is_loopback(host):
        return None
    for category, patterns in _HOSTS.items():
        for pattern in patterns:
            if pattern.startswith("."):
                if host.endswith(pattern) or host == pattern[1:]:
                    return category
            elif host == pattern:
                return category
    return C.OTHER


class NetworkPolicy:
    def __init__(self, config: DFMConfig) -> None:
        self._config = config

    def rule(self, category: NetworkCategory) -> Decision:
        return Decision(getattr(self._config.network, category.value))

    def check(self, category: NetworkCategory | None, target: str) -> PolicyVerdict:
        if category is None:
            return PolicyVerdict(
                Decision.ALLOW, ActionClass.SAFE_LOCAL, f"{target}: loopback", approved_by="policy"
            )
        decision = self.rule(category)
        return PolicyVerdict(
            decision,
            ActionClass.REMOTE_READ,
            f"network.{category.value} = {decision}",
            confirmation=Confirmation.NORMAL if decision is Decision.ASK else Confirmation.NONE,
            network=category,
            approved_by=None if decision is Decision.ASK else "policy",
        )

    def check_target(self, target: str) -> PolicyVerdict:
        return self.check(categorize(target), target)


def network_action(
    target: str,
    proposed_by: str,
    action_class: ActionClass = ActionClass.REMOTE_READ,
    category: NetworkCategory | None = None,
) -> ProposedAction:
    """A ProposedAction for contacting `target`; loopback becomes SAFE_LOCAL."""
    category = category if category is not None else categorize(target)
    if category is None:
        return ProposedAction("network", ActionClass.SAFE_LOCAL, target, proposed_by, target=target)
    return ProposedAction(
        "network", action_class, target, proposed_by, target=host_of(target), network=category
    )
