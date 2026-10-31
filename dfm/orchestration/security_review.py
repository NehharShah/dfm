"""`dfm security-review`: a security-focused review of a diff.

Same discipline as `dfm review` (044): deterministic findings first, then an
optional model pass whose findings must quote evidence that verifies against the
diff. On top of that:

- per-language and per-category checklists, kept as data (`dfm/security/checklists/*.toml`):
  injection, authz, secrets, crypto, deserialization, SSRF, path traversal, CI
  workflows, IaC / Kubernetes RBAC, smart contracts;
- authorization checks that the diff removes;
- dependency manifests parsed before and after (the base ref vs the working tree), so
  added, changed and removed packages are listed exactly;
- RepoKG's structural diff: new import edges and ops-surface changes.

Advisory databases are never fetched (that is network access DFM does not have here);
the report says so instead.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, cast

from dfm.integrations.repokg.structure import graph_at_ref, roles_from_canon, structural_diff
from dfm.models.base import Model, ModelError
from dfm.orchestration.review import (
    SEVERITY_ORDER,
    FileChange,
    Finding,
    ReviewReport,
    Severity,
    model_findings,
    parse_diff,
)
from dfm.orchestration.runtime import Runtime
from dfm.repository.intelligence import gather
from dfm.secrets import redact
from dfm.security.injection import DATA_RULE

CHECKLISTS = Path(__file__).resolve().parent.parent / "security" / "checklists"
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|testdata|fixtures)/|(^|/)test_[^/]+\.py$|_test\.(go|py|rs)$|\.(test|spec)\.[jt]sx?$|\.t\.sol$"
)
# Categories that matter even in test code (a key in a fixture is still a leaked key).
_ALWAYS = {"secrets", "ci", "iac"}


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    severity: Severity
    title: str
    pattern: re.Pattern[str]
    why: str
    files: re.Pattern[str]
    unless: re.Pattern[str] | None = None


@cache
def load_rules(directory: Path = CHECKLISTS) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    for path in sorted(directory.glob("*.toml")):
        data = tomllib.loads(path.read_text())
        files = re.compile(str(data.get("files", ".*")))
        for raw in cast(list[dict[str, Any]], data.get("rule", [])):
            severity = str(raw["severity"])
            if severity not in SEVERITY_ORDER:
                raise ValueError(f"{path.name}:{raw.get('id')}: bad severity {severity}")
            unless = raw.get("unless")
            rules.append(
                Rule(
                    str(raw["id"]),
                    str(raw["category"]),
                    cast(Severity, severity),
                    str(raw["title"]),
                    re.compile(str(raw["pattern"])),
                    str(raw["why"]),
                    re.compile(str(raw["files"])) if "files" in raw else files,
                    re.compile(str(unless)) if unless else None,
                )
            )
    return tuple(rules)


# ---------------------------------------------------------------- checklist findings

_AUTH = re.compile(
    r"@(login_required|permission_required|requires_auth|authenticated|PreAuthorize|Secured|RolesAllowed|UseGuards)\b"
    r"|\b(require_auth|requireAuth|authorize|isAuthenticated|ensureAuthenticated|checkPermission|has_permission|verify_token|verifyToken|onlyOwner|onlyRole|auth_required)\b"
    r"|\brequire\(\s*msg\.sender\s*=="
    r"|\bassert\(\s*get_caller_address\(\)\s*=="
)


def checklist_findings(changes: list[FileChange], rules: tuple[Rule, ...]) -> list[Finding]:
    out: list[Finding] = []
    for change in changes:
        in_tests = bool(_TEST_PATH.search(change.path))
        applicable = [
            r
            for r in rules
            if r.files.search(change.path) and (not in_tests or r.category in _ALWAYS)
        ]
        previous: dict[int, str] = dict(change.added)
        for line, text in change.added:
            # `unless` may sit on the line itself or just above it (e.g. a // SAFETY: comment)
            around = "\n".join(previous.get(n, "") for n in (line - 2, line - 1)) + "\n" + text
            matched = [
                r
                for r in applicable
                if r.pattern.search(text) and not (r.unless and r.unless.search(around))
            ]
            secret = bool(redact(text).count)
            for rule in matched:
                evidence = "[redacted]" if secret else text  # secrets never reach the report
                out.append(
                    Finding(
                        rule.severity,
                        rule.title,
                        change.path,
                        line,
                        evidence,
                        rule.why,
                        rule.category,
                    )
                )
            if secret and not any(r.category == "secrets" for r in matched):
                out.append(
                    Finding(
                        "HIGH",
                        "possible secret added",
                        change.path,
                        line,
                        "[redacted]",
                        "move it to the environment or a secret store, and rotate it if it was real",
                        "secrets",
                    )
                )
        if not in_tests:
            out += _removed_auth(change)
    return out


def _removed_auth(change: FileChange) -> list[Finding]:
    """An authorization check present before the change and gone after it."""
    kept = {m.group(0) for _, text in change.added for m in _AUTH.finditer(text)}
    findings: list[Finding] = []
    for text in change.removed:
        match = _AUTH.search(text)
        if match and match.group(0) not in kept:
            findings.append(
                Finding(
                    "HIGH",
                    "authorization check removed",
                    change.path,
                    None,
                    text,
                    f"`{match.group(0)}` is no longer present; confirm the endpoint or function is still protected",
                    "authz",
                )
            )
    return findings


# ---------------------------------------------------------------- dependency changes


@dataclass(frozen=True)
class DependencyChange:
    manifest: str
    name: str
    before: str | None
    after: str | None

    @property
    def kind(self) -> str:
        if self.before is None:
            return "added"
        return "removed" if self.after is None else "changed"

    def describe(self) -> str:
        if self.kind == "added":
            return f"{self.manifest}: + {self.name} {self.after}".rstrip()
        if self.kind == "removed":
            return f"{self.manifest}: - {self.name}"
        return f"{self.manifest}: {self.name} {self.before} -> {self.after}"


def _pep508_name(spec: str) -> tuple[str, str]:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)", spec)
    if not match:
        return spec.strip(), ""
    return match.group(1).lower().replace("_", "-"), match.group(3).strip()


def parse_dependencies(name: str, text: str) -> dict[str, str]:
    """Package → version spec for one manifest's content ({} if unparseable)."""
    deps: dict[str, str] = {}
    try:
        if name == "package.json":
            data = cast(dict[str, Any], json.loads(text))
            for key in (
                "dependencies",
                "devDependencies",
                "optionalDependencies",
                "peerDependencies",
            ):
                deps.update({k: str(v) for k, v in cast(dict[str, Any], data.get(key, {})).items()})
        elif name == "pyproject.toml":
            data = tomllib.loads(text)
            project = cast(dict[str, Any], data.get("project", {}))
            specs = list(cast(list[str], project.get("dependencies", [])))
            for group in cast(
                dict[str, list[str]], project.get("optional-dependencies", {})
            ).values():
                specs += group
            for group in cast(dict[str, list[Any]], data.get("dependency-groups", {})).values():
                specs += [g for g in group if isinstance(g, str)]
            deps.update(dict(_pep508_name(s) for s in specs))
            poetry = cast(
                dict[str, Any], cast(dict[str, Any], data.get("tool", {})).get("poetry", {})
            )
            for k, v in cast(dict[str, Any], poetry.get("dependencies", {})).items():
                if k != "python":
                    deps[k.lower()] = json.dumps(v) if isinstance(v, dict) else str(v)
        elif re.fullmatch(r"requirements[\w.-]*\.txt", name):
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-"):
                    package, spec = _pep508_name(line)
                    deps[package] = spec
        elif name in {"Cargo.toml", "Scarb.toml"}:
            data = tomllib.loads(text)
            tables = [
                data.get(k, {}) for k in ("dependencies", "dev-dependencies", "build-dependencies")
            ]
            tables.append(cast(dict[str, Any], data.get("workspace", {})).get("dependencies", {}))
            for table in tables:
                for k, v in cast(dict[str, Any], table).items():
                    deps[k] = json.dumps(v, sort_keys=True) if isinstance(v, dict) else str(v)
        elif name == "go.mod":
            for match in re.finditer(
                r"^\s*(?:require\s+)?([\w.~/-]+\.[\w.~/-]+)\s+(v[\w.+-]+)", text, re.M
            ):
                deps[match.group(1)] = match.group(2)
        elif name == "Package.swift":
            for match in re.finditer(
                r'\.package\(\s*(?:name:\s*"[^"]*",\s*)?url:\s*"([^"]+)"\s*,\s*([^)]*)\)', text
            ):
                deps[match.group(1)] = match.group(2).strip()
    except (ValueError, tomllib.TOMLDecodeError):
        return {}
    return deps


MANIFEST = re.compile(
    r"(^|/)(package\.json|pyproject\.toml|requirements[\w.-]*\.txt|Cargo\.toml|Scarb\.toml|go\.mod|Package\.swift)$"
)


def _at_ref(repo: Path, ref: str, path: str) -> str:
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return out.stdout if out.returncode == 0 else ""


def dependency_changes(
    repo: Path, changes: list[FileChange], base: str = "HEAD"
) -> list[DependencyChange]:
    out: list[DependencyChange] = []
    for change in changes:
        if not MANIFEST.search(change.path):
            continue
        name = Path(change.path).name
        before = parse_dependencies(name, _at_ref(repo, base, change.path))
        current = repo / change.path
        after = (
            parse_dependencies(name, current.read_text(errors="replace"))
            if current.is_file()
            else {}
        )
        for package in sorted(before.keys() | after.keys()):
            if before.get(package) != after.get(package):
                out.append(
                    DependencyChange(change.path, package, before.get(package), after.get(package))
                )
    return out


_UNPINNED_SOURCE = re.compile(r"git\+|github:|\bgit\s*[=:]|https?://|file:|link:|\*$|^latest$")


def dependency_findings(deps: list[DependencyChange]) -> list[Finding]:
    out: list[Finding] = []
    for dep in deps:
        if dep.kind == "removed":
            continue
        spec = dep.after or ""
        if _UNPINNED_SOURCE.search(spec):
            out.append(
                Finding(
                    "MEDIUM",
                    "dependency from a URL, git or unpinned source",
                    dep.manifest,
                    None,
                    f"{dep.name} {spec}",
                    "these bypass the registry's immutability; pin to a released version or a commit",
                    "dependencies",
                )
            )
        elif dep.kind == "added":
            out.append(
                Finding(
                    "LOW",
                    "new dependency",
                    dep.manifest,
                    None,
                    f"{dep.name} {spec}".strip(),
                    "new code in the supply chain: check maintainer, popularity and install scripts",
                    "dependencies",
                )
            )
    return out


# ---------------------------------------------------------------- entry point

SECURITY_INSTRUCTIONS = (
    "You are a security reviewer looking at a code diff. Report only exploitable or clearly "
    "risky problems you can point to: missing or broken authorization, injection (SQL, shell, "
    "template, XSS), secrets, crypto misuse, unsafe deserialization, SSRF, path traversal, "
    "insecure defaults, CI/workflow permission changes, infrastructure or RBAC exposure, "
    "smart-contract issues (reentrancy, access control, unchecked calls). Every finding must "
    "quote an exact line from the diff as evidence and give the new-file line number. Return "
    "an empty list when there is nothing. Do not invent findings. " + DATA_RULE
)


async def security_review(
    rt: Runtime | None,
    diff_text: str,
    *,
    repo: Path | None = None,
    base: str = "HEAD",
    model: Model | None = None,
) -> ReviewReport:
    repo = repo or (rt.repo if rt else None)
    changes = parse_diff(diff_text)
    findings = checklist_findings(changes, load_rules())
    report = ReviewReport(
        findings,
        [c.path for c in changes],
        sum(len(c.added) for c in changes),
        sum(len(c.removed) for c in changes),
    )
    if repo is not None:
        deps = dependency_changes(repo, changes, base)
        report.findings += dependency_findings(deps)
        report.sections["Dependency changes"] = [d.describe() for d in deps]
        if deps:
            report.notes.append(
                "vulnerability advisories were not checked (that needs network access)"
            )
    if rt is not None:
        rt.event("user_request", text="security review of the diff")
        intel = await gather(rt.repo, rt.canon, rt.graph)
        if intel.graph is not None:
            before = await graph_at_ref(rt.repo, base)
            after = await rt.graph.refresh(rt.repo)
            if before is not None and after is not None:
                delta = structural_diff(before, after, roles_from_canon(intel.canon))
                report.sections["Ops surface changes (RepoKG)"] = delta.ops_changes
                report.sections["New import edges (RepoKG)"] = delta.added_edges[:30]
                report.findings += [
                    Finding(
                        "LOW",
                        "ops surface changed",
                        None,
                        None,
                        change,
                        "new or changed entry points, routes, jobs or config are attack surface: check authn/authz and input validation",
                        "exposure",
                    )
                    for change in delta.ops_changes
                    if change.startswith(("added", "changed"))
                ]
        else:
            report.notes.append("RepoKG unavailable: ops surface and import edges not analyzed")
        if model is None:
            try:
                model = rt.pool.coder("primary")
            except ModelError:
                model = None
    if model is not None and changes:
        kept, dropped = await model_findings(
            model, changes, diff_text, repo, 6000, SECURITY_INSTRUCTIONS
        )
        report.findings += kept
        report.dropped = dropped
    elif changes:
        report.notes.append("no local coding model: only the checklists ran")
    if rt is not None:
        rt.event(
            "verification",
            check="security-review",
            ok=not any(f.severity in {"BLOCKER", "HIGH"} for f in report.findings),
            findings=len(report.findings),
            dropped=report.dropped,
        )
    return report
