import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dfm.cli.app import app
from dfm.orchestration.review import FileChange
from dfm.orchestration.runtime import Runtime
from dfm.orchestration.security_review import (
    checklist_findings,
    dependency_changes,
    load_rules,
    parse_dependencies,
    security_review,
)
from tests import repo_fixtures as fx
from tests.helpers import ScriptedCoder

# One line per rule that the rule must flag (path decides which checklist applies).
POSITIVE: dict[str, tuple[str, str]] = {
    "private-key": ("deploy/key.pem", "-----BEGIN OPENSSH PRIVATE KEY-----"),
    "disable-tls": ("app/client.py", "requests.get(url, verify=False)"),
    "weak-hash": ("app/auth.py", "digest = hashlib.md5(password).hexdigest()"),
    "cors-any": ("app/server.py", 'app.add_middleware(CORSMiddleware, allow_origins=["*"])'),
    "bind-all": ("app/main.py", 'uvicorn.run(app, host="0.0.0.0")'),
    "py-sql-format": ("app/db.py", 'cur.execute(f"SELECT * FROM users WHERE id = {uid}")'),
    "py-shell": ("app/run.py", 'os.system(f"convert {name} out.png")'),
    "py-eval": ("app/calc.py", "result = eval(expression)"),
    "py-pickle": ("app/cache.py", "obj = pickle.loads(blob)"),
    "py-ssrf": ("app/proxy.py", 'resp = requests.get(request.args["url"])'),
    "py-path-traversal": (
        "app/files.py",
        'return send_file(os.path.join(ROOT, request.args["f"]))',
    ),
    "py-jwt-noverify": ("app/auth.py", 'jwt.decode(tok, options={"verify_signature": False})'),
    "py-debug-server": ("app/main.py", "app.run(debug=True)"),
    "py-insecure-random": ("app/auth.py", "reset_token = str(random.randint(0, 10**6))"),
    "py-mktemp": ("app/io.py", "path = tempfile.mktemp()"),
    "js-exec": ("src/run.ts", "exec(`convert ${name} out.png`)"),
    "js-eval": ("src/calc.js", "const f = new Function(body)"),
    "js-sql-template": ("src/db.ts", "await db.query(`SELECT * FROM t WHERE id = ${id}`)"),
    "js-xss": ("src/view.tsx", "<div dangerouslySetInnerHTML={{ __html: html }} />"),
    "js-open-redirect": ("src/routes.js", "res.redirect(req.query.next)"),
    "js-path-traversal": ("src/files.js", "res.sendFile(path.join(root, req.params.name))"),
    "js-ssrf": ("src/proxy.ts", "const r = await fetch(req.query.url)"),
    "js-jwt-decode": ("src/auth.ts", "const claims = jwt.decode(token)"),
    "js-insecure-random": ("src/auth.ts", "const resetToken = Math.random().toString(36)"),
    "js-prototype-pollution": ("src/api.js", "_.merge(config, req.body)"),
    "go-sql-sprintf": (
        "db/q.go",
        'rows, _ := db.Query(fmt.Sprintf("SELECT * FROM t WHERE id=%s", id))',
    ),
    "go-shell": ("cmd/run.go", 'exec.Command("sh", "-c", "convert "+name)'),
    "go-path-traversal": ("web/files.go", "http.ServeFile(w, r, filepath.Join(root, r.URL.Path))"),
    "go-ssrf": ("web/proxy.go", 'resp, _ := http.Get(r.URL.Query().Get("u"))'),
    "go-math-rand": ("auth/token.go", '\t"math/rand"'),
    "rs-unsafe": ("src/buf.rs", "unsafe { ptr.write(0) }"),
    "rs-shell": ("src/run.rs", 'Command::new("sh").arg("-c").arg(cmd).spawn()'),
    "rs-sql-format": ("src/db.rs", 'conn.execute(&format!("DELETE FROM t WHERE id={}", id), [])'),
    "sol-tx-origin": ("src/Vault.sol", "require(tx.origin == owner);"),
    "sol-delegatecall": ("src/Proxy.sol", "(bool ok, ) = impl.delegatecall(data);"),
    "sol-selfdestruct": ("src/Kill.sol", "selfdestruct(payable(owner));"),
    "sol-low-level-call": ("src/Vault.sol", '(bool ok, ) = to.call{value: amount}("");'),
    "sol-timestamp-randomness": (
        "src/Lottery.sol",
        "uint r = uint(keccak256(abi.encode(block.timestamp)));",
    ),
    "sol-unchecked": ("src/Math.sol", "unchecked { i++; }"),
    "cairo-tx-origin": (
        "src/lib.cairo",
        "let who = get_tx_info().unbox().account_contract_address;",
    ),
    "cairo-library-call": ("src/lib.cairo", "replace_class_syscall(new_class).unwrap();"),
    "ci-prt": (".github/workflows/ci.yml", "on: pull_request_target"),
    "ci-write-perms": (".github/workflows/ci.yml", "  contents: write"),
    "ci-script-injection": (
        ".github/workflows/ci.yml",
        '  run: echo "${{ github.event.issue.title }}"',
    ),
    "ci-unpinned-action": (".github/workflows/ci.yml", "    - uses: someone/setup-tool@v2"),
    "ci-curl-pipe": (
        ".github/workflows/ci.yml",
        "  run: curl -sSL https://x.example/install.sh | bash",
    ),
    "k8s-privileged": ("k8s/deploy.yaml", "        privileged: true"),
    "k8s-root": ("k8s/deploy.yaml", "        runAsUser: 0"),
    "k8s-rbac-wildcard": ("k8s/rbac.yaml", '  verbs: ["*"]'),
    "iam-wildcard": ("infra/policy.json", '  "Action": "*",'),
    "net-open-ingress": ("infra/sg.tf", '  cidr_blocks = ["0.0.0.0/0"]'),
    "storage-public": ("infra/bucket.tf", '  acl    = "public-read"'),
}

# Lines that must not be flagged (common false positives).
NEGATIVE = [
    ("app/calc.py", "value = ast.literal_eval(text)"),
    ("app/cache.py", "etag = hashlib.md5(body, usedforsecurity=False).hexdigest()"),
    ("app/db.py", 'cur.execute("SELECT * FROM users WHERE id = %s", (uid,))'),
    ("src/auth.ts", "const claims = jwt.verify(token, key)"),
    (".github/workflows/ci.yml", "    - uses: actions/checkout@v4"),
    ("src/buf.rs", "// SAFETY: ptr is valid for writes\nunsafe { ptr.write(0) }"),
    ("tests/test_db.py", 'cur.execute(f"SELECT 1")'),  # test code: language rules skipped
]


def change(path: str, *lines: str, removed: tuple[str, ...] = ()) -> FileChange:
    return FileChange(
        path, added=[(i + 1, ln) for i, ln in enumerate(lines)], removed=list(removed)
    )


def test_checklists_load_and_ids_are_unique() -> None:
    rules = load_rules()
    ids = [r.id for r in rules]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(POSITIVE), "every rule needs a positive sample (and vice versa)"
    assert {r.category for r in rules} >= {
        "injection",
        "authz",
        "secrets",
        "crypto",
        "deserialization",
        "ssrf",
        "path-traversal",
        "ci",
        "iac",
        "smart-contract",
    }


@pytest.mark.parametrize("rule_id", sorted(POSITIVE))
def test_every_rule_flags_its_sample(rule_id: str) -> None:
    rules = load_rules()
    rule = next(r for r in rules if r.id == rule_id)
    path, line = POSITIVE[rule_id]
    findings = checklist_findings([change(path, line)], rules)
    assert any(f.title == rule.title for f in findings), (rule_id, findings)


@pytest.mark.parametrize(("path", "text"), NEGATIVE)
def test_known_false_positives_stay_quiet(path: str, text: str) -> None:
    lines = text.split("\n")
    findings = checklist_findings([change(path, *lines)], load_rules())
    assert [f for f in findings if f.line == len(lines)] == [], findings


def test_removed_authorization_is_flagged() -> None:
    gone = change("app/views.py", "def delete_user(uid):", removed=("@login_required",))
    moved = change("app/views.py", "@login_required", removed=("@login_required",))
    rules = load_rules()
    assert [f.title for f in checklist_findings([gone], rules)] == ["authorization check removed"]
    assert checklist_findings([moved], rules) == []
    owner = change("src/Vault.sol", "function withdraw() external {", removed=("    onlyOwner",))
    assert checklist_findings([owner], rules)[0].category == "authz"


def test_dependency_parsing_per_ecosystem() -> None:
    assert parse_dependencies(
        "package.json",
        json.dumps({"dependencies": {"left-pad": "^1"}, "devDependencies": {"vitest": "3"}}),
    ) == {"left-pad": "^1", "vitest": "3"}
    assert parse_dependencies(
        "pyproject.toml", '[project]\ndependencies = ["Requests[socks]>=2", "typer"]\n'
    ) == {"requests": ">=2", "typer": ""}
    assert parse_dependencies("requirements-dev.txt", "pytest==8.0  # tests\n-r base.txt\n") == {
        "pytest": "==8.0"
    }
    assert parse_dependencies(
        "Cargo.toml",
        '[dependencies]\nserde = "1"\n[dev-dependencies]\nrand = { version = "0.8" }\n',
    ) == {"serde": "1", "rand": '{"version": "0.8"}'}
    assert parse_dependencies(
        "go.mod",
        "module x\n\nrequire (\n\tgolang.org/x/net v0.20.0\n)\nrequire github.com/a/b v1.2.3\n",
    ) == {"golang.org/x/net": "v0.20.0", "github.com/a/b": "v1.2.3"}
    assert parse_dependencies("package.json", "{not json") == {}


def regression_repo(root: Path, git: Callable[..., str]) -> Path:
    """Committed baseline, then a working-tree change that introduces one problem per area."""
    repo = fx.write(
        root,
        {
            "app/views.py": "from auth import login_required\n\n\n@login_required\ndef delete_user(uid):\n    return db.delete(uid)\n",
            "package.json": json.dumps({"dependencies": {"express": "^4.19.0"}}, indent=2),
            ".github/workflows/ci.yml": "on: push\npermissions:\n  contents: read\n",
            "k8s/deploy.yaml": "spec:\n  containers:\n    - name: app\n",
        },
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    fx.write(
        repo,
        {
            "app/views.py": 'from auth import login_required\n\n\ndef delete_user(uid):\n    return db.execute(f"DELETE FROM users WHERE id = {uid}")\n',
            "package.json": json.dumps(
                {"dependencies": {"express": "^4.21.0", "evil-pkg": "github:someone/evil"}},
                indent=2,
            ),
            ".github/workflows/ci.yml": "on: pull_request_target\npermissions:\n  contents: write\n",
            "k8s/deploy.yaml": "spec:\n  containers:\n    - name: app\n      securityContext:\n        privileged: true\n",
        },
    )
    return repo


def diff(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-color", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


async def test_security_regression_fixture_is_detected(
    tmp_path: Path, git: Callable[..., str]
) -> None:
    repo = regression_repo(tmp_path / "r", git)
    deps = dependency_changes(
        repo, __import__("dfm.orchestration.review", fromlist=["parse_diff"]).parse_diff(diff(repo))
    )
    assert [d.describe() for d in deps] == [
        "package.json: + evil-pkg github:someone/evil",
        "package.json: express ^4.19.0 -> ^4.21.0",
    ]
    report = await security_review(None, diff(repo), repo=repo)
    titles = {(f.category, f.title) for f in report.findings}
    assert ("authz", "authorization check removed") in titles
    assert ("injection", "SQL built with string formatting") in titles
    assert ("ci", "pull_request_target trigger") in titles
    assert ("ci", "CI token permissions widened") in titles
    assert ("iac", "privileged container") in titles
    assert ("dependencies", "dependency from a URL, git or unpinned source") in titles
    assert report.sections["Dependency changes"]
    assert any("advisories were not checked" in n for n in report.notes)
    assert "authorization check removed" in report.render()


async def test_clean_diff_has_no_high_findings(tmp_path: Path, git: Callable[..., str]) -> None:
    repo = fx.write(tmp_path / "c", {"app/util.py": "def add(a, b):\n    return a + b\n"})
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    (repo / "app/util.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    report = await security_review(None, diff(repo), repo=repo)
    assert not [f for f in report.findings if f.severity in {"BLOCKER", "HIGH", "MEDIUM"}]


async def test_runtime_path_with_verified_model_findings(
    home: Path, tmp_path: Path, git: Callable[..., str]
) -> None:
    repo = regression_repo(tmp_path / "r", git)
    real = {
        "severity": "HIGH",
        "file": "app/views.py",
        "line": 5,
        "title": "SQL injection in delete_user",
        "evidence": 'return db.execute(f"DELETE FROM users WHERE id = {uid}")',
        "reasoning": "uid flows into SQL unescaped",
        "category": "security",
    }
    invented = {**real, "line": 2, "title": "made up", "evidence": "os.system(user_input)"}
    rt = Runtime.create(repo, request="security review", mode="review")
    report = await security_review(
        rt, diff(repo), model=ScriptedCoder({"findings": [real, invented]})
    )
    await rt.close("completed")
    assert [f.title for f in report.findings if f.source == "model"] == [
        "SQL injection in delete_user"
    ]
    assert report.dropped == 1
    assert "Ops surface changes (RepoKG)" in report.sections or any(
        "RepoKG" in n for n in report.notes
    )


def test_cli_exit_code_and_json(home: Path, tmp_path: Path, git: Callable[..., str]) -> None:
    repo = regression_repo(tmp_path / "r", git)
    result = CliRunner().invoke(
        app, ["security-review", "--repo", str(repo), "--json", "--stdin"], input=diff(repo)
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert any(f["title"] == "privileged container" for f in payload["findings"])
    assert payload["sections"]["Dependency changes"]


def test_secret_lines_never_reach_the_report() -> None:
    key = "-----BEGIN OPENSSH PRIVATE KEY-----"
    findings = checklist_findings([change("deploy/key.pem", key)], load_rules())
    assert findings and all(f.evidence == "[redacted]" for f in findings)
    assert findings[0].severity == "BLOCKER"
