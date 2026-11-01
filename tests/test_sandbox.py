import secrets
import socket
import sys
from pathlib import Path

import pytest

from dfm.config import ConfigOverrides, DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.policy import Policy
from dfm.processes import ProcessManager, ProcessRegistry
from dfm.security.sandbox import SANDBOX_EXEC, Sandbox, available, build_profile
from dfm.tools.shell.classify import classify
from dfm.tools.shell.runner import CommandRunner

live = pytest.mark.skipif(not available(), reason="sandbox-exec not available")


def make_env(tmp_path: Path, home: Path, offline: bool = False) -> ExecutionEnvironment:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    environ = {"HOME": str(home), "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
    return ExecutionEnvironment.create(
        repo,
        DFMConfig(),
        session_id="sb",
        environ=environ,
        dirs=state_dirs({"HOME": str(tmp_path / "dfmhome")}),
        overrides=ConfigOverrides(offline=offline),
    )


def test_profile_text(tmp_path: Path) -> None:
    env = make_env(tmp_path, tmp_path / "home")
    closed = build_profile(env, network=False).text
    assert "(deny network-outbound)" in closed and 'remote ip "localhost:*"' in closed
    assert ".ssh" in closed and "Library/Keychains" in closed
    assert str(env.repo_root) in closed
    assert "(deny network-outbound)" not in build_profile(env, network=True).text


def test_quoting(tmp_path: Path) -> None:
    weird = tmp_path / 'we"ird\\dir'
    weird.mkdir()
    env = make_env(tmp_path, tmp_path / "home")
    text = build_profile(env, network=False, extra_write=[weird]).text
    assert 'we\\"ird\\\\dir' in text


def test_wrap_network_decision(tmp_path: Path) -> None:
    sandbox = Sandbox()
    sandbox.enabled = True
    env = make_env(tmp_path, tmp_path / "home")
    tests = sandbox.wrap(["pytest"], env, classify("pytest"))
    assert tests[0] == SANDBOX_EXEC and "(deny network-outbound)" in tests[2]
    fetch = sandbox.wrap(["cargo", "fetch"], env, classify("cargo fetch"))
    assert "(deny network-outbound)" not in fetch[2]
    offline = make_env(tmp_path, tmp_path / "home", offline=True)
    assert (
        "(deny network-outbound)"
        in sandbox.wrap(["cargo", "fetch"], offline, classify("cargo fetch"))[2]
    )
    sandbox.enabled = False
    assert sandbox.wrap(["pytest"], env, classify("pytest")) == ["pytest"]


async def run_sandboxed(env: ExecutionEnvironment, code: str) -> tuple[int | None, str]:
    runner = CommandRunner(
        env,
        ProcessManager(ProcessRegistry(env.tmp / "p")),
        Policy(DFMConfig()),
        wrap=Sandbox().wrap,
    )
    script = (
        env.repo_root / f"probe_{secrets.token_hex(3)}.py"
    )  # repository code, as a test would be
    script.write_text(code)
    outcome = await runner.run([sys.executable, script.name])
    assert outcome.result is not None
    return outcome.result.exit_code, outcome.result.stdout + outcome.result.stderr


@live
async def test_repository_code_cannot_read_keys(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATE")
    env = make_env(tmp_path, home)
    code, out = await run_sandboxed(env, f"print(open({str(home / '.ssh/id_rsa')!r}).read())")
    assert code != 0 and "PRIVATE" not in out and "not permitted" in out


@live
async def test_repository_code_cannot_rewrite_dfm_state_or_git_hooks(tmp_path: Path) -> None:
    env = make_env(tmp_path, tmp_path / "home")
    for d in (*env.state_roots, env.repo_root / ".git/hooks", env.repo_root / ".git/objects"):
        d.mkdir(parents=True, exist_ok=True)
    (env.repo_root / ".git/config").write_text("[core]\n")
    config_dir, data_dir = env.state_roots[1], env.state_roots[0]
    targets = {
        "dfm config": config_dir / "config.toml",
        "dfm registry": data_dir / "forged.json",
        "git hook": env.repo_root / ".git/hooks/pre-commit",
        "git config": env.repo_root / ".git/config",
        "repo file": env.repo_root / "ok.txt",
        "git objects": env.repo_root / ".git/objects/ok",
        "session tmp": env.tmp / "ok.txt",
    }
    probe = (
        "import sys\n"
        f"for name, path in {[(k, str(v)) for k, v in targets.items()]!r}:\n"
        "    try:\n"
        "        open(path, 'a').write('x'); print(name, 'WROTE')\n"
        "    except OSError:\n"
        "        print(name, 'DENIED')\n"
    )
    _, out = await run_sandboxed(env, probe)
    results = dict(line.rsplit(" ", 1) for line in out.strip().splitlines())
    assert results == {
        "dfm config": "DENIED",
        "dfm registry": "DENIED",
        "git hook": "DENIED",
        "git config": "DENIED",
        "repo file": "WROTE",
        "git objects": "WROTE",
        "session tmp": "WROTE",
    }


@live
async def test_repository_code_cannot_write_outside(tmp_path: Path) -> None:
    probe = Path.home() / f"dfm-sandbox-probe-{secrets.token_hex(4)}"
    try:
        env = make_env(tmp_path, tmp_path / "home")
        code, _ = await run_sandboxed(env, f"open({str(probe)!r}, 'w').write('x')")
        assert code != 0 and not probe.exists()
        ok, out = await run_sandboxed(env, "open('inside.txt', 'w').write('x'); print('ok')")
        assert ok == 0 and "ok" in out
    finally:
        probe.unlink(missing_ok=True)


@live
async def test_network_blocked_loopback_allowed(tmp_path: Path) -> None:
    env = make_env(tmp_path, tmp_path / "home")
    code, _ = await run_sandboxed(
        env, "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    )
    assert code != 0
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    try:
        ok, out = await run_sandboxed(
            env,
            f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=3); print('loop ok')",
        )
        assert ok == 0 and "loop ok" in out
    finally:
        server.close()


def test_read_only_repo_profile(tmp_path: Path) -> None:
    env = make_env(tmp_path, tmp_path / "home")
    text = build_profile(env, network=False, read_only_repo=True).text
    assert f'(deny file-write* (subpath "{env.repo_root}"))' in text.splitlines()


@live
async def test_read_only_repo_blocks_writes_even_under_temp(tmp_path: Path) -> None:
    env = make_env(tmp_path, tmp_path / "home")
    runner = CommandRunner(
        env,
        ProcessManager(ProcessRegistry(env.tmp / "p")),
        Policy(DFMConfig()),
        wrap=Sandbox(read_only_repo=True).wrap,
    )
    script = env.repo_root / "probe.py"
    script.write_text("open('created.txt', 'w').write('x')\n")
    outcome = await runner.run([sys.executable, "probe.py"])
    assert outcome.result is not None and outcome.result.exit_code != 0
    assert not (env.repo_root / "created.txt").exists()


def test_network_only_when_every_code_part_is_approved_for_it(tmp_path: Path) -> None:
    sandbox = Sandbox()
    sandbox.enabled = True
    env = make_env(tmp_path, tmp_path / "home")
    fetch = sandbox.wrap(["git", "fetch"], env, classify("git fetch"))
    assert "(deny network-outbound)" not in fetch[2]
    mixed = "git fetch && python exfil.py"
    wrapped = sandbox.wrap(["bash", "-c", mixed], env, classify(f"bash -c '{mixed}'"))
    assert "(deny network-outbound)" in wrapped[2]  # the script must not ride on the fetch
    reads = "git fetch && cat README.md"
    assert (
        "(deny network-outbound)"
        not in sandbox.wrap(["bash", "-c", reads], env, classify(f"bash -c '{reads}'"))[2]
    )
    # a read-only tool that can start a program (`awk | "sh"`, `sed e`) does not ride on the
    # fetch's approval either, whatever its script looked like to the rules
    for tool in ("awk '{print $1}' f", "sed -n 1p f", "less f", "find . -name x"):
        script = f"git fetch && {tool}"
        assert (
            "(deny network-outbound)"
            in sandbox.wrap(["bash", "-c", script], env, classify(f"bash -c {script!r}"))[2]
        ), tool


def test_dfm_private_paths_are_write_denied_after_the_allow_rule(tmp_path: Path) -> None:
    env = make_env(tmp_path, tmp_path / "home")
    lines = build_profile(env, network=False).text.splitlines()
    allow = next(i for i, x in enumerate(lines) if x.startswith("(deny file-write* (require-not"))
    private = next(i for i, x in enumerate(lines) if f'"{env.repo_root}/.dfm"' in x)
    assert private > allow, "later SBPL rules win: the deny must follow the toolchain allow"
    assert str(Path(sys.prefix).resolve()) in lines[private]
    assert str(Path(sys.base_prefix).resolve()) in lines[private]
    assert f"{env.home}/.cache/dfm" in lines[private]
    # the worker's own profile re-allows its KV directory, nothing else under ~/.cache/dfm
    kv = env.home / ".cache" / "dfm" / "kv"
    worker = build_profile(env, network=False, extra_write=[kv]).text.splitlines()
    kv_allow = worker.index(f'(allow file-write* (subpath "{kv}"))')
    kv_deny = next(i for i, x in enumerate(worker) if f'"{env.home}/.cache/dfm"' in x)
    assert kv_allow > kv_deny


@live
async def test_repository_code_cannot_write_what_dfm_trusts_later(tmp_path: Path) -> None:
    env = make_env(tmp_path, tmp_path / "home")
    (env.repo_root / ".dfm" / "originals").mkdir(parents=True)
    (env.home / ".cache" / "dfm" / "kv").mkdir(parents=True)
    (env.home / ".cache" / "other").mkdir(parents=True)
    targets = {
        "checkpoint original": env.repo_root / ".dfm" / "originals" / "deadbeef",
        "repo policy": env.repo_root / ".dfm" / "config.toml",
        "kv cache": env.home / ".cache" / "dfm" / "kv" / "x.safetensors",
        "other cache": env.home / ".cache" / "other" / "ok",
        "interpreter": Path(sys.prefix) / "lib" / "dfm-sandbox-probe-sitecustomize.py",
        "repo file": env.repo_root / "ok.txt",
    }
    probe = (
        f"for name, path in {[(k, str(v)) for k, v in targets.items()]!r}:\n"
        "    try:\n"
        "        open(path, 'a').write('x'); print(name, 'WROTE')\n"
        "    except OSError:\n"
        "        print(name, 'DENIED')\n"
    )
    try:
        _, out = await run_sandboxed(env, probe)
    finally:
        targets["interpreter"].unlink(missing_ok=True)
    results = dict(line.rsplit(" ", 1) for line in out.strip().splitlines())
    assert results == {
        "checkpoint original": "DENIED",
        "repo policy": "DENIED",
        "kv cache": "DENIED",
        "other cache": "WROTE",
        "interpreter": "DENIED",
        "repo file": "WROTE",
    }


@live
async def test_inside_a_sandbox_no_second_one_is_applied(tmp_path: Path) -> None:
    from dfm.security.sandbox import already_sandboxed

    assert not already_sandboxed()  # the test runner itself is not sandboxed
    env = make_env(tmp_path, tmp_path / "home")
    probe = (
        "from dfm.security.sandbox import Sandbox, already_sandboxed, available\n"
        "print(already_sandboxed(), available(), Sandbox().enabled)\n"
    )
    _, out = await run_sandboxed(
        env, f"import sys; sys.path.insert(0, {str(Path.cwd())!r})\n{probe}"
    )
    assert out.strip().splitlines()[-1] == "True False False"
