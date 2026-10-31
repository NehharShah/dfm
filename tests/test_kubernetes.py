from pathlib import Path

import pytest

from dfm.config import DFMConfig, load_config, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.policy import Confirmation, Decision, InteractiveApprover, Policy
from dfm.policy.types import PolicyVerdict, ProposedAction
from dfm.processes import ProcessManager, ProcessRegistry
from dfm.tools.kubernetes import kube_context
from dfm.tools.shell.classify import classify
from dfm.tools.shell.runner import CommandRunner

KUBECONFIG = """\
apiVersion: v1
kind: Config
current-context: kind-dev
contexts:
- name: kind-dev
  context: {cluster: kind-dev, user: dev, namespace: team-a}
- name: prod-eu
  context: {cluster: prod-eu, user: dev}
clusters:
- name: kind-dev
  cluster: {server: "https://127.0.0.1:6443"}
- name: prod-eu
  cluster: {server: "https://10.0.0.1:6443"}
users:
- name: dev
  user: {token: "not-a-real-token"}
"""

TABLE = [
    ("helm template app ./chart", "SAFE_LOCAL", None),
    ("kubectl get pods", "REMOTE_READ", "kubernetes"),
    ("kubectl describe secret db", "REMOTE_READ", "kubernetes"),
    ("kubectl diff -f k8s/", "REMOTE_READ", "kubernetes"),
    ("kubectl apply -f k8s/", "REMOTE_WRITE", "kubernetes"),
    ("helm upgrade app ./chart", "REMOTE_WRITE", "kubernetes"),
    ("kubectl delete deploy app", "DESTRUCTIVE_REMOTE_WRITE", "kubernetes"),
    ("helm uninstall app", "DESTRUCTIVE_REMOTE_WRITE", "kubernetes"),
    ("kubectl config view --raw", "PRIVILEGED", None),
    ("kubectl config view", "SAFE_READ", None),
    ("kubectl get secret db -o yaml", "PRIVILEGED", "kubernetes"),
    ("kubectl get secrets -o jsonpath={.items[*].data}", "PRIVILEGED", "kubernetes"),
]


@pytest.mark.parametrize(("command", "action", "network"), TABLE)
def test_classification(command: str, action: str, network: str | None) -> None:
    part = classify(command).parts[0]
    assert part.action.value == action
    assert (part.network.value if part.network else None) == network


def evaluate(command: str, target: str | None, config: DFMConfig | None = None) -> PolicyVerdict:
    (action,) = classify(command).to_actions("dfm")
    action = ProposedAction(**{**action.__dict__, "target": target})
    return Policy(config or DFMConfig()).evaluate(action)


def test_cluster_writes_need_a_hard_confirmation_naming_the_context() -> None:
    verdict = evaluate("kubectl apply -f k8s/", "kind-dev/team-a")
    assert (verdict.decision, verdict.confirmation) == (Decision.ASK, Confirmation.HARD)
    assert evaluate("kubectl apply -f k8s/", None).decision is Decision.DENY  # unknown context
    allow_all = DFMConfig.model_validate({"network": {"kubernetes": "allow"}})
    assert (
        evaluate("kubectl apply -f k8s/", "kind-dev/team-a", allow_all).confirmation
        is Confirmation.HARD
    )
    assert evaluate("kubectl get pods", "kind-dev/team-a", allow_all).decision is Decision.ALLOW


def test_production_contexts_deny_writes_unless_allowed() -> None:
    for target in ("prod-eu/default", "dev/production", "live", "eu-prd-1/x"):
        verdict = evaluate("helm upgrade app ./chart", target)
        assert verdict.decision is Decision.DENY and "production-looking" in verdict.reason, target
    assert evaluate("kubectl apply -f k8s/", "products-dev/default").decision is Decision.ASK
    opted_in = DFMConfig.model_validate({"security": {"allow_production_cluster_writes": True}})
    assert (
        evaluate("kubectl apply -f k8s/", "prod-eu/default", opted_in).confirmation
        is Confirmation.HARD
    )


def test_repo_config_cannot_allow_production_writes(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    (repo / ".dfm").mkdir(parents=True)
    (repo / ".dfm" / "config.toml").write_text(
        "[security]\nallow_production_cluster_writes = true\n"
    )
    loaded = load_config(repo, env={"HOME": str(tmp_path / "home")})
    assert loaded.config.security.allow_production_cluster_writes is False
    assert any("may only tighten" in w for w in loaded.warnings)


async def test_apply_blocked_without_hard_confirmation() -> None:
    (action,) = classify("kubectl apply -f k8s/").to_actions("dfm")
    action = ProposedAction(**{**action.__dict__, "target": "kind-dev/team-a"})
    answers = iter(["y", "kind-dev/team-a"])
    approver = InteractiveApprover(input_fn=lambda _: next(answers), is_tty=lambda: True)
    policy = Policy(DFMConfig(), approver)
    assert (await policy.authorize(action)).decision is Decision.DENY  # "y" is not enough
    assert (await policy.authorize(action)).decision is Decision.ALLOW  # typing the context is


@pytest.fixture
def kubeconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "kubeconfig"
    path.write_text(KUBECONFIG)
    monkeypatch.setenv("KUBECONFIG", str(path))
    return path


@pytest.mark.skipif(__import__("shutil").which("kubectl") is None, reason="kubectl not installed")
def test_kube_context_reads_names_only(kubeconfig: Path) -> None:
    ctx = kube_context()
    assert ctx is not None and (ctx.name, ctx.namespace) == ("kind-dev", "team-a")
    other = kube_context("prod-eu")
    assert other is not None and other.namespace == "default"
    assert "not-a-real-token" not in repr(ctx)


@pytest.mark.skipif(__import__("shutil").which("kubectl") is None, reason="kubectl not installed")
async def test_namespace_inside_bash_c_and_explicit_kubeconfig_decide_the_target(
    kubeconfig: Path, tmp_path: Path
) -> None:
    environ = {
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
    }
    env = ExecutionEnvironment.create(
        tmp_path, DFMConfig(), session_id="k8s2", environ=environ, dirs=state_dirs(environ)
    )
    seen: list[ProposedAction] = []
    policy = Policy(DFMConfig())
    policy.subscribe(lambda action, verdict: seen.append(action))
    runner = CommandRunner(env, ProcessManager(ProcessRegistry(tmp_path / "p")), policy)
    # no KUBECONFIG in the command's environment: only --kubeconfig names the cluster
    await runner.run(f"bash -c 'kubectl --kubeconfig {kubeconfig} -n production delete deploy x'")
    assert any(a.target == "kind-dev/production" for a in seen), [a.target for a in seen]


@pytest.mark.skipif(__import__("shutil").which("kubectl") is None, reason="kubectl not installed")
async def test_context_is_shown_before_cluster_commands(kubeconfig: Path, tmp_path: Path) -> None:
    environ = {
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "KUBECONFIG": str(kubeconfig),  # the context shown is the one kubectl itself will use
    }
    env = ExecutionEnvironment.create(
        tmp_path, DFMConfig(), session_id="k8s", environ=environ, dirs=state_dirs(environ)
    )
    events: list[tuple[str, dict[str, object]]] = []
    seen: list[ProposedAction] = []
    policy = Policy(DFMConfig())  # no human: cluster reads (ask) and writes are refused
    policy.subscribe(lambda action, verdict: seen.append(action))
    runner = CommandRunner(
        env,
        ProcessManager(ProcessRegistry(env.tmp / "p")),
        policy,
        on_event=lambda t, **kw: events.append((t, kw)),
    )
    read = await runner.run("kubectl get pods -n payments")
    write = await runner.run("kubectl --context prod-eu apply -f k8s/")
    assert read.result is None and write.result is None  # nothing reached a cluster
    notes = [kw["text"] for t, kw in events if t == "note"]
    assert "kube context kind-dev, namespace payments" in notes
    assert "kube context prod-eu, namespace default" in notes
    assert (
        seen[0].summary.startswith("[kind-dev/payments] ") and seen[0].target == "kind-dev/payments"
    )
    assert "production-looking" in write.verdicts[-1].reason
