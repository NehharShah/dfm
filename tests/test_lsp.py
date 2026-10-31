import shutil
import sys
from pathlib import Path

import pytest

from dfm.config import DFMConfig, state_dirs
from dfm.execution import ExecutionEnvironment
from dfm.lsp import LspClient, LspError
from dfm.tools.languages import LspServerSpec, PythonAdapter
from tests import repo_fixtures as fx

FAKE_SERVER = r"""
import json, sys

def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        line = line.decode().strip()
        if not line:
            break
        if line.lower().startswith("content-length:"):
            length = int(line.split(":")[1])
    return json.loads(sys.stdin.buffer.read(length))

def send(msg):
    body = json.dumps(msg).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()

def loc(uri, line, col):
    return {"uri": uri, "range": {"start": {"line": line, "character": col}, "end": {"line": line, "character": col + 3}}}

CRASH = "--crash" in sys.argv
while True:
    msg = read()
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": 900, "method": "workspace/configuration", "params": {"items": [{}]}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"definitionProvider": True}}})
    elif method == "textDocument/didOpen":
        uri = params["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": [
            {"range": {"start": {"line": 1, "character": 4}, "end": {"line": 1, "character": 9}}, "severity": 1, "message": "bad return", "code": "E1"}]}})
    elif CRASH and method == "textDocument/definition":
        sys.exit(3)
    elif method == "textDocument/definition":
        send({"jsonrpc": "2.0", "id": mid, "result": [loc(params["textDocument"]["uri"], 0, 4)]})
    elif method == "textDocument/references":
        uri = params["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": mid, "result": [loc(uri, 0, 4), loc(uri, 5, 8)]})
    elif method == "textDocument/hover":
        send({"jsonrpc": "2.0", "id": mid, "result": {"contents": {"kind": "markdown", "value": "def add(a, b)"}}})
    elif method == "textDocument/documentSymbol":
        r = {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 0}}
        send({"jsonrpc": "2.0", "id": mid, "result": [{"name": "add", "kind": 12, "range": r, "selectionRange": r, "children": []}]})
    elif method == "textDocument/rename":
        uri = params["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": mid, "result": {"changes": {uri: [{"range": loc(uri, 0, 4)["range"], "newText": params["newName"]}]}}})
    elif method == "textDocument/implementation":
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not supported"}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "exit":
        sys.exit(0)
    elif mid is not None and "method" in msg:
        send({"jsonrpc": "2.0", "id": mid, "result": None})
"""


@pytest.fixture
def env(tmp_path: Path) -> ExecutionEnvironment:
    repo = fx.write(
        tmp_path / "r", {"calc.py": "def add(a, b):\n    return a - b\n\n\nx = 1\ny = add(1, 2)\n"}
    )
    # The pip `pyright` wrapper needs node: give it the real one, or it downloads Node.js
    # (nodeenv) into this temporary HOME on every run.
    node = shutil.which("node")
    node_dir = f"{Path(node).parent}:" if node else ""
    environ = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{Path(sys.executable).parent}:{node_dir}/usr/bin:/bin",
    }
    return ExecutionEnvironment.create(
        repo, DFMConfig(), session_id="lsp", environ=environ, dirs=state_dirs(environ)
    )


def fake_spec(tmp_path: Path, *extra: str) -> LspServerSpec:
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER)
    return LspServerSpec("Python", (sys.executable, str(script), *extra), "python", (".py",))


async def test_operations_against_fake_server(env: ExecutionEnvironment, tmp_path: Path) -> None:
    async with LspClient(fake_spec(tmp_path), env) as client:
        assert client.capabilities == {"definitionProvider": True}
        definition = await client.definition("calc.py", 6, 5)
        assert [str(d) for d in definition] == ["calc.py:1:5"]
        refs = await client.references("calc.py", 1, 5)
        assert [r.line for r in refs] == [1, 6]
        assert await client.hover("calc.py", 1, 5) == "def add(a, b)"
        symbols = await client.document_symbols("calc.py")
        assert [(s.name, s.kind) for s in symbols] == [("add", "function")]
        preview = await client.rename_preview("calc.py", 1, 5, "plus")
        assert preview.files == ["calc.py"] and preview.edits["calc.py"][0][1] == "plus"
        assert (env.repo_root / "calc.py").read_text().startswith("def add")  # preview only
        diags = await client.diagnostics("calc.py", wait=5)
        assert [(d.line, d.column, d.message, d.code) for d in diags] == [
            (2, 5, "bad return", "E1")
        ]
        with pytest.raises(LspError, match="not supported"):
            await client.implementation("calc.py", 1, 5)


async def test_crash_and_missing_server(env: ExecutionEnvironment, tmp_path: Path) -> None:
    client = LspClient(fake_spec(tmp_path, "--crash"), env, request_timeout=5)
    await client.start()
    with pytest.raises(LspError):
        await client.definition("calc.py", 1, 5)
    await client.close()
    missing = LspClient(LspServerSpec("Python", ("no-such-lsp",), "python", (".py",)), env)
    with pytest.raises(LspError, match="not found"):
        await missing.start()


async def test_paths_stay_in_boundary(env: ExecutionEnvironment, tmp_path: Path) -> None:
    async with LspClient(fake_spec(tmp_path), env) as client:
        with pytest.raises(PermissionError):
            await client.definition("../../etc/passwd", 1, 1)


@pytest.mark.lsp
@pytest.mark.skipif(PythonAdapter().lsp_server() is None, reason="no Python LSP server installed")
async def test_live_pyright(env: ExecutionEnvironment) -> None:
    spec = PythonAdapter().lsp_server()
    assert spec is not None
    fx.write(env.repo_root, {"use.py": "from calc import add\n\nprint(add(2, 3))\n"})
    async with LspClient(spec, env, request_timeout=60) as client:
        definition = await client.definition("use.py", 3, 7)
        assert definition and definition[0].path == "calc.py" and definition[0].line == 1
        refs = await client.references("calc.py", 1, 5)
        assert {r.path for r in refs} >= {"calc.py", "use.py"}
        symbols = await client.document_symbols("calc.py")
        assert "add" in [s.name for s in symbols]


def test_spec_resolution() -> None:
    spec = PythonAdapter().lsp_server()
    if spec is not None:
        assert shutil.which(spec.argv[0]) is not None
