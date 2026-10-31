"""Which chain a command targets: local, testnet or mainnet.

From, in order: an explicit chain id (`--chain`, `--chain-id`), a network name
(`--network sepolia`), the RPC URL (`--rpc-url`, `--url`, `--node`, `--fork-url`).
Anything not recognised is treated as mainnet: an unknown endpoint may hold real funds.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from dfm.policy.network import is_loopback
from dfm.policy.types import KEY_MATERIAL, NetworkCategory

if TYPE_CHECKING:
    from dfm.tools.shell.classify import Classification

ChainKind = Literal["local", "testnet", "mainnet"]

CHAIN_IDS: dict[int, tuple[ChainKind, str]] = {
    1: ("mainnet", "ethereum"),
    10: ("mainnet", "optimism"),
    56: ("mainnet", "bnb"),
    100: ("mainnet", "gnosis"),
    137: ("mainnet", "polygon"),
    250: ("mainnet", "fantom"),
    324: ("mainnet", "zksync"),
    8453: ("mainnet", "base"),
    42161: ("mainnet", "arbitrum"),
    43114: ("mainnet", "avalanche"),
    59144: ("mainnet", "linea"),
    534352: ("mainnet", "scroll"),
    5: ("testnet", "goerli"),
    17000: ("testnet", "holesky"),
    560048: ("testnet", "hoodi"),
    11155111: ("testnet", "sepolia"),
    80002: ("testnet", "polygon-amoy"),
    84532: ("testnet", "base-sepolia"),
    421614: ("testnet", "arbitrum-sepolia"),
    11155420: ("testnet", "optimism-sepolia"),
    43113: ("testnet", "avalanche-fuji"),
    97: ("testnet", "bnb-testnet"),
    31337: ("local", "anvil"),
    1337: ("local", "dev"),
}
_EXTRA_NAMES: dict[str, tuple[ChainKind, str]] = {
    "mainnet": ("mainnet", "ethereum"),
    "eth": ("mainnet", "ethereum"),
    "localhost": ("local", "localhost"),
    "devnet": ("local", "devnet"),
    "katana": ("local", "katana"),
}
CHAIN_NAMES: dict[str, tuple[ChainKind, str]] = {
    **{name: (kind, name) for kind, name in CHAIN_IDS.values()},
    **_EXTRA_NAMES,
}
_TESTNET_WORDS = re.compile(
    r"(sepolia|holesky|hoodi|goerli|testnet|amoy|fuji|chiado|alfajores|devnet|staging|test)", re.I
)
_MAINNET_WORDS = re.compile(r"mainnet|^eth[.-]|ethereum|polygon-rpc|arb1|base\.org", re.I)


@dataclass(frozen=True)
class ChainTarget:
    kind: ChainKind
    name: str
    source: str  # chain-id | network | rpc-url | unknown

    @property
    def label(self) -> str:
        """`kind:name`, the phrase a hard confirmation asks the developer to type."""
        return f"{self.kind}:{self.name}"


def _option(argv: Sequence[str], *names: str) -> str | None:
    for i, arg in enumerate(argv):
        for name in names:
            if arg == name and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def from_chain_id(value: str) -> ChainTarget | None:
    text = value.strip().lower()
    if text.isdigit():
        kind, name = CHAIN_IDS.get(int(text), ("mainnet", f"chain-{text}"))
        return ChainTarget(kind, name, "chain-id")
    if text in CHAIN_NAMES:
        kind, name = CHAIN_NAMES[text]
        return ChainTarget(kind, name, "chain-id")
    if re.search(r"local|testing|^dev", text):  # cosmos: localnet, testing
        return ChainTarget("local", text, "chain-id")
    if _TESTNET_WORDS.search(text):
        return ChainTarget("testnet", text, "chain-id")
    return ChainTarget("mainnet", text, "chain-id")  # e.g. cosmoshub-4, osmosis-1


# testnet names as whole labels of the RPC host (eth-sepolia.g.alchemy.com, rpc.testnet.x);
# never the path or query, where an API key or project name may contain anything
_TESTNET_LABELS = frozenset(
    {
        "sepolia",
        "holesky",
        "hoodi",
        "goerli",
        "testnet",
        "amoy",
        "fuji",
        "chiado",
        "alfajores",
        "devnet",
    }
)
_SEVERITY = {"local": 0, "testnet": 1, "mainnet": 2}


def from_url(url: str) -> ChainTarget:
    host = (urlparse(url).hostname or "").lower()
    if host and is_loopback(host):
        return ChainTarget("local", host, "rpc-url")
    labels: set[str] = set(re.split(r"[.-]", host)) if host else set()
    testnet = sorted(labels & _TESTNET_LABELS)
    if testnet:
        return ChainTarget("testnet", testnet[0], "rpc-url")
    # known mainnet, unknown, or unparsable: the same caution
    return ChainTarget("mainnet", host or "unknown", "rpc-url")


def chain_target(argv: Sequence[str]) -> ChainTarget:
    """Where a command lands. With several hints (chain id, network, RPC URL) the riskiest wins:
    `--chain 31337 --rpc-url https://mainnet...` is a mainnet transaction."""
    found: list[ChainTarget] = []
    chain = _option(argv, "--chain", "--chain-id")
    if chain:
        target = from_chain_id(chain)
        if target is not None:
            found.append(target)
    network = _option(argv, "--network")
    if network:
        found.append(from_chain_id(network) or ChainTarget("mainnet", network, "network"))
    url = _option(argv, "--rpc-url", "-r", "--url", "--rpc", "--node", "--fork-url")
    if url is not None:
        found.append(from_url(url))
    if not found:
        return ChainTarget("mainnet", "unknown", "unknown")
    return max(found, key=lambda t: _SEVERITY.get(t.kind, 2))


@dataclass(frozen=True)
class ChainAction:
    target: ChainTarget
    write: bool  # signs/broadcasts a transaction
    key_material: bool  # private key / mnemonic / keystore handling


def classify_chain_action(classification: Classification) -> ChainAction | None:
    """The chain side of a classified command (None when it does not touch a chain)."""

    for part in classification.parts:
        keys = KEY_MATERIAL in part.reasons
        chain_net = part.network in {
            NetworkCategory.BLOCKCHAIN_RPC_READ,
            NetworkCategory.BLOCKCHAIN_RPC_WRITE,
        }
        local_tx = "local chain" in " ".join(part.reasons) or "local devnet" in " ".join(
            part.reasons
        )
        if chain_net or keys or local_tx:
            target = (
                chain_target(part.argv)
                if not local_tx
                else ChainTarget("local", "localhost", "rpc-url")
            )
            write = part.network is NetworkCategory.BLOCKCHAIN_RPC_WRITE or (
                local_tx
                and any(
                    w in " ".join(part.reasons)
                    for w in ("broadcast", "deploys", "send", "invoke", "tx")
                )
            )
            return ChainAction(target, write, keys)
    return None
