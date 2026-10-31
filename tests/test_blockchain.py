import pytest

from dfm.config import DFMConfig
from dfm.policy import Confirmation, Decision, InteractiveApprover, Policy
from dfm.policy.types import PolicyVerdict
from dfm.tools.blockchain import chain_target, classify_chain_action
from dfm.tools.shell.classify import classify

KEY = "0x" + "ab" * 32
ALCHEMY_MAIN = "https://eth-mainnet.g.alchemy.com/v2/KEY"
ALCHEMY_SEPOLIA = "https://eth-sepolia.g.alchemy.com/v2/KEY"

TABLE = [
    # command, action class, network, chain label
    (
        "cast send 0xA 'f()' --rpc-url http://127.0.0.1:8545 --unlocked --from 0x1",
        "SAFE_LOCAL",
        None,
        None,
    ),
    (
        f"cast send 0xA 'f()' --rpc-url {ALCHEMY_SEPOLIA} --account dev",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "testnet:sepolia",
    ),
    (
        f"cast send 0xA 'f()' --rpc-url {ALCHEMY_MAIN} --account dev",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "mainnet:eth-mainnet.g.alchemy.com",
    ),
    (
        "cast send 0xA 'f()' --rpc-url https://rpc.example.org --account dev",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "mainnet:rpc.example.org",
    ),
    (
        "cast send 0xA 'f()' --chain 11155111 --account dev",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "testnet:sepolia",
    ),
    (
        "cast send 0xA 'f()' --account dev",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "mainnet:unknown",
    ),
    (
        f"cast call 0xA 'f()' --rpc-url {ALCHEMY_MAIN}",
        "REMOTE_READ",
        "blockchain_rpc_read",
        "mainnet:eth-mainnet.g.alchemy.com",
    ),
    ("cast balance 0xA --rpc-url http://localhost:8545", "SAFE_LOCAL", None, None),
    (
        "forge script s.s.sol --rpc-url https://sepolia.infura.io/v3/x --broadcast --account d",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "testnet:sepolia",
    ),
    ("forge script s.s.sol --rpc-url http://127.0.0.1:8545 --broadcast", "SAFE_LOCAL", None, None),
    ("forge script s.s.sol", "SAFE_BUILD", None, None),
    (
        "sncast --account dev invoke --network sepolia --contract-address 0x1 --function f",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "testnet:sepolia",
    ),
    (
        "starkli invoke 0x1 f --network mainnet",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "mainnet:ethereum",
    ),
    (
        "gaiad tx bank send a b 1uatom --chain-id cosmoshub-4 --node https://rpc.cosmos.network:443",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "mainnet:cosmoshub-4",
    ),
    (
        "osmosisd tx gamm swap --chain-id osmo-test-5 --node https://rpc.testnet.osmosis.zone",
        "REMOTE_WRITE",
        "blockchain_rpc_write",
        "testnet:osmo-test-5",
    ),
    (
        "gaiad tx bank send a b 1stake --chain-id localnet --node http://127.0.0.1:26657",
        "SAFE_LOCAL",
        None,
        None,
    ),
    (
        "gaiad query bank balances x --node https://rpc.cosmos.network:443",
        "REMOTE_READ",
        "blockchain_rpc_read",
        "mainnet:rpc.cosmos.network",
    ),
    ("gaiad keys add me --recover", "PRIVILEGED", None, None),
    ("gaiad keys list", "SAFE_READ", None, None),
    (
        f"cast send 0xA 'f()' --rpc-url {ALCHEMY_SEPOLIA} --private-key {KEY}",
        "PRIVILEGED",
        None,
        None,
    ),
    ("cast wallet new", "PRIVILEGED", None, None),
]


@pytest.mark.parametrize(("command", "action", "network", "chain"), TABLE)
def test_classification_table(
    command: str, action: str, network: str | None, chain: str | None
) -> None:
    part = classify(command).parts[0]
    assert part.action.value == action
    assert (part.network.value if part.network else None) == network
    if chain is not None:
        assert part.target == chain


def test_chain_detection() -> None:
    assert chain_target(["cast", "send", "--chain", "1"]).label == "mainnet:ethereum"
    assert chain_target(["cast", "send", "--chain", "31337"]).kind == "local"
    assert chain_target(["cast", "send", "--chain", "999999"]).label == "mainnet:chain-999999"
    assert chain_target(["x", "--rpc-url", "https://base-sepolia.example/rpc"]).kind == "testnet"
    action = classify_chain_action(
        classify(f"cast send 0xA f() --rpc-url {ALCHEMY_MAIN} --account d")
    )
    assert action is not None and action.write and action.target.kind == "mainnet"
    assert classify_chain_action(classify("ls")) is None


def verdict(command: str, config: DFMConfig, proposed_by: str = "user") -> PolicyVerdict:
    (action,) = classify(command).to_actions(proposed_by)
    return Policy(config).evaluate(action)


WRITES_ASK = DFMConfig.model_validate({"network": {"blockchain_rpc_write": "ask"}})
WRITES_ALLOW = DFMConfig.model_validate({"network": {"blockchain_rpc_write": "allow"}})


def test_transaction_policy_tiers() -> None:
    local = verdict(
        "cast send 0xA f() --rpc-url http://127.0.0.1:8545 --unlocked --from 0x1", DFMConfig()
    )
    assert local.decision is Decision.ALLOW  # local transactions are fine
    main = f"cast send 0xA f() --rpc-url {ALCHEMY_MAIN} --account dev"
    test = f"cast send 0xA f() --rpc-url {ALCHEMY_SEPOLIA} --account dev"
    assert (
        verdict(main, DFMConfig()).decision is Decision.DENY
    )  # blockchain_rpc_write = deny by default
    assert (verdict(test, WRITES_ASK).decision, verdict(test, WRITES_ASK).confirmation) == (
        Decision.ASK,
        Confirmation.NORMAL,
    )
    assert (verdict(main, WRITES_ASK).decision, verdict(main, WRITES_ASK).confirmation) == (
        Decision.ASK,
        Confirmation.HARD,
    )
    # signing is never automatic, even with the network rule at allow
    assert verdict(main, WRITES_ALLOW).confirmation is Confirmation.HARD
    assert verdict(test, WRITES_ALLOW).decision is Decision.ASK
    read = verdict(
        f"cast call 0xA f() --rpc-url {ALCHEMY_MAIN}",
        DFMConfig.model_validate({"network": {"blockchain_rpc_read": "allow"}}),
    )
    assert read.decision is Decision.ALLOW


def test_private_keys_from_models_are_denied() -> None:
    command = f"cast send 0xA f() --rpc-url {ALCHEMY_SEPOLIA} --private-key {KEY}"
    assert verdict(command, WRITES_ALLOW, proposed_by="model:coder").decision is Decision.DENY
    user = verdict(command, WRITES_ALLOW, proposed_by="user")
    assert (user.decision, user.confirmation) == (Decision.ASK, Confirmation.HARD)


async def test_mainnet_send_denied_without_hard_confirmation() -> None:
    (action,) = classify(f"cast send 0xA f() --rpc-url {ALCHEMY_MAIN} --account dev").to_actions(
        "user"
    )
    answers = iter(["y", "mainnet:eth-mainnet.g.alchemy.com"])
    policy = Policy(
        WRITES_ASK, InteractiveApprover(input_fn=lambda _: next(answers), is_tty=lambda: True)
    )
    assert (await policy.authorize(action)).decision is Decision.DENY
    assert (await policy.authorize(action)).decision is Decision.ALLOW


async def test_keys_are_redacted_in_traces(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from dfm.config import state_dirs
    from dfm.execution import ExecutionEnvironment
    from dfm.processes import ProcessManager, ProcessRegistry
    from dfm.tools.shell.runner import CommandRunner

    environ = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    env = ExecutionEnvironment.create(
        tmp_path, DFMConfig(), session_id="c", environ=environ, dirs=state_dirs(environ)
    )
    events: list[tuple[str, dict[str, object]]] = []
    runner = CommandRunner(
        env,
        ProcessManager(ProcessRegistry(env.tmp / "p")),
        Policy(DFMConfig()),
        on_event=lambda t, **kw: events.append((t, kw)),
        proposed_by="model:coder",
    )
    outcome = await runner.run(
        ["cast", "send", "0xA", "f()", "--rpc-url", ALCHEMY_SEPOLIA, "--private-key", KEY]
    )
    assert outcome.result is None and "proposed by a model" in outcome.verdicts[-1].reason
    assert KEY not in repr(events)


def test_action_summaries_never_carry_key_material() -> None:
    """The approver prompt and the session trace show `ProposedAction.summary`."""
    for argv in (
        ["cast", "send", "0xA", "--private-key", KEY, "--mnemonic", "word word"],
        ["cast", "send", "0xA", f"--private-key={KEY}"],
        ["forge", "script", "s", "--mnemonics", "a b c", "--private-keys", KEY],
    ):
        (action,) = classify(argv).to_actions("model:coder")
        assert KEY not in action.summary and "word word" not in action.summary
        assert "a b c" not in action.summary and "[REDACTED]" in action.summary


def test_the_riskiest_chain_hint_wins_and_testnets_come_from_the_host() -> None:
    from dfm.tools.blockchain.chains import chain_target

    mixed = ["cast", "send", "--chain", "31337", "--rpc-url", "https://mainnet.infura.io/v3/x"]
    assert chain_target(mixed).label == "mainnet:mainnet.infura.io"
    keyed = ["cast", "send", "--rpc-url", "https://mainnet.example.com/test/key"]
    assert chain_target(keyed).kind == "mainnet"  # "test" in the path is not a testnet
    assert (
        chain_target(["cast", "send", "--rpc-url", "https://eth-sepolia.g.alchemy.com/v2/k"]).kind
        == "testnet"
    )
    assert chain_target(["cast", "send", "--rpc-url="]).label == "mainnet:unknown"
