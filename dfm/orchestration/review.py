"""`dfm review`: findings about a diff, with no invented ones.

Two sources of findings:
- deterministic checks over the added lines (secrets, risky constructs, CI
  permission changes, generated-file edits, debug leftovers, missing tests) and
  RepoKG's structural diff (architecture warnings);
- an optional model pass, whose findings must quote evidence that appears in
  the diff or the current file at the stated location. Anything that does not
  verify is dropped, and the count of dropped findings is reported.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from dfm.integrations.repokg.structure import graph_at_ref, roles_from_canon, structural_diff
from dfm.models.base import GenerateRequest, Model, ModelError
from dfm.models.structured import structured
from dfm.orchestration.runtime import Runtime
from dfm.repository.intelligence import gather
from dfm.secrets import redact, redact_diff
from dfm.security.injection import DATA_RULE, untrusted_block
from dfm.tools.generated import generated_info

Severity = Literal["BLOCKER", "HIGH", "MEDIUM", "LOW", "NIT"]
SEVERITY_ORDER = {"BLOCKER": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NIT": 4}


@dataclass(frozen=True)
class Finding:
    severity: Severity
    title: str
    file: str | None
    line: int | None
    evidence: str
    reasoning: str
    category: str
    source: str = "deterministic"

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.file and self.line else (self.file or "diff")


@dataclass
class ReviewReport:
    findings: list[Finding]
    files: list[str]
    lines_added: int
    lines_removed: int
    dropped: int = 0
    notes: list[str] = field(default_factory=lambda: [])
    architecture: str | None = None
    sections: dict[str, list[str]] = field(default_factory=lambda: {})  # titled extra lists

    def render(self) -> str:
        lines = [f"Reviewed {len(self.files)} files (+{self.lines_added} -{self.lines_removed})."]
        if not self.findings:
            lines.append("No findings.")
        for f in sorted(self.findings, key=lambda x: (SEVERITY_ORDER[x.severity], x.location)):
            lines.append(f"\n[{f.severity}] {f.title} ({f.location}, {f.category}, {f.source})")
            if f.evidence:
                lines.append(f"    > {f.evidence.strip()[:200]}")
            lines.append(f"    {f.reasoning}")
        if self.architecture:
            lines.append(f"\nArchitecture:\n{self.architecture}")
        for title, items in self.sections.items():
            lines.append(f"\n{title}:")
            lines.extend(f"- {item}" for item in items or ["(none)"])
        if self.dropped:
            lines.append(
                f"\n{self.dropped} model finding(s) dropped: their evidence did not match the code."
            )
        lines.extend(f"\nNote: {n}" for n in self.notes)
        return "\n".join(lines)


# ---------------------------------------------------------------- diff parsing


@dataclass
class FileChange:
    path: str
    added: list[tuple[int, str]] = field(default_factory=lambda: [])  # (new line number, text)
    removed: list[str] = field(default_factory=lambda: [])
    context: list[str] = field(default_factory=lambda: [])  # first unchanged lines seen (headers)
    new_file: bool = False
    deleted: bool = False
    binary: bool = False


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff(text: str) -> list[FileChange]:
    files: list[FileChange] = []
    current: FileChange | None = None
    new_line = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = FileChange(match.group(2) if match else line.split()[-1])
            files.append(current)
            continue
        if current is None:
            if line.startswith("+++ "):
                current = FileChange(line[4:].removeprefix("b/").strip())
                files.append(current)
            continue
        if line.startswith("new file mode"):
            current.new_file = True
        elif line.startswith("deleted file mode"):
            current.deleted = True
        elif line.startswith("Binary files"):
            current.binary = True
        elif line.startswith("+++ "):
            target = line[4:].strip()
            if target != "/dev/null":
                current.path = target.removeprefix("b/")
        elif line.startswith("--- "):
            continue
        elif m := _HUNK.match(line):
            new_line = int(m.group(1))
        elif line.startswith("+"):
            current.added.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith("-"):
            current.removed.append(line[1:])
        elif line.startswith(" ") or line == "":
            if len(current.context) < 10:
                current.context.append(line[1:])
            new_line += 1
    return files


# ---------------------------------------------------------------- deterministic checks

_TEST_FILE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|_test\.(go|py|rs)$|\.(test|spec)\.[jt]sx?$|\.t\.sol$"
)
_GENERATED = re.compile(
    r"(Code generated .* DO NOT EDIT|@generated|DO NOT EDIT|autogenerated|auto-generated)", re.I
)
_LOCKFILES = {
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
    "bun.lock",
    "Package.resolved",
}
_MANIFESTS = {
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Package.swift",
    "requirements.txt",
}

RISKY: list[tuple[re.Pattern[str], Severity, str, str]] = [
    (
        re.compile(r"\beval\("),
        "MEDIUM",
        "eval of dynamic input",
        "eval runs arbitrary code if any part of its input is attacker-controlled",
    ),
    (re.compile(r"\bexec\("), "MEDIUM", "exec of dynamic code", "exec runs arbitrary code"),
    (
        re.compile(r"shell\s*=\s*True"),
        "MEDIUM",
        "subprocess with shell=True",
        "shell=True enables command injection when arguments include user input",
    ),
    (
        re.compile(r"\bos\.system\("),
        "MEDIUM",
        "os.system call",
        "os.system passes a string to the shell",
    ),
    (
        re.compile(r"\bpickle\.loads?\("),
        "MEDIUM",
        "pickle deserialization",
        "unpickling untrusted data executes code",
    ),
    (
        re.compile(r"\byaml\.load\((?![^)]*Loader=\s*(yaml\.)?SafeLoader)"),
        "MEDIUM",
        "yaml.load without SafeLoader",
        "yaml.load can construct arbitrary objects",
    ),
    (
        re.compile(r"verify\s*=\s*False|InsecureSkipVerify:\s*true|rejectUnauthorized:\s*false"),
        "HIGH",
        "TLS verification disabled",
        "disabling certificate checks allows man-in-the-middle attacks",
    ),
    (
        re.compile(r"\.innerHTML\s*=|dangerouslySetInnerHTML"),
        "MEDIUM",
        "raw HTML injection",
        "unescaped HTML enables XSS",
    ),
    (
        re.compile(r"\bunsafe\s*\{"),
        "LOW",
        "unsafe block",
        "unsafe code needs a justification comment and review",
    ),
    (
        re.compile(r"\bmd5\b|\bsha1\b", re.I),
        "LOW",
        "weak hash",
        "MD5/SHA-1 are unsuitable for security purposes",
    ),
]
DEBUG = re.compile(
    r"^\s*(print\(|console\.log\(|dbg!\(|fmt\.Println\(|breakpoint\(\)|import pdb|pdb\.set_trace\(\)|debugger;)"
)
TODO = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")


def deterministic_findings(changes: list[FileChange], repo: Path | None) -> list[Finding]:
    out: list[Finding] = []
    source_changed = tests_changed = False
    changed_names = {Path(c.path).name for c in changes}
    for change in changes:
        path = change.path
        is_test = bool(_TEST_FILE.search(path))
        tests_changed |= is_test
        source_changed |= not is_test and Path(path).suffix in {
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".go",
            ".rs",
            ".swift",
            ".sol",
            ".cairo",
        }
        if change.binary:
            out.append(
                Finding(
                    "LOW",
                    "binary file changed",
                    path,
                    None,
                    "",
                    "binary changes cannot be reviewed here; check they belong in the repository",
                    "maintainability",
                )
            )
        head = "\n".join([*change.context[:5], *(t for _, t in change.added[:5])])
        if repo is not None and (repo / path).is_file() and not change.new_file:
            with contextlib.suppress(OSError):
                head = (repo / path).read_text(errors="replace")[:600]
        generated = _GENERATED.search(head) is not None or (
            repo is not None
            and (repo / path).is_file()
            and generated_info(repo / path, repo) is not None
        )
        if generated and change.added:
            out.append(
                Finding(
                    "MEDIUM",
                    "generated file edited by hand",
                    path,
                    change.added[0][0],
                    change.added[0][1],
                    "the next code generation run will overwrite this; change the generator input instead",
                    "maintainability",
                )
            )
        if Path(path).name in _LOCKFILES and not (_MANIFESTS & changed_names):
            out.append(
                Finding(
                    "LOW",
                    "lockfile changed without a manifest change",
                    path,
                    None,
                    "",
                    "dependency versions moved without an intentional manifest edit",
                    "dependencies",
                )
            )
        if path.startswith(".github/workflows/"):
            for line, text in change.added:
                if re.search(
                    r"pull_request_target|permissions:\s*write-all|contents:\s*write|id-token:\s*write",
                    text,
                ):
                    out.append(
                        Finding(
                            "HIGH",
                            "CI permissions widened",
                            path,
                            line,
                            text,
                            "workflow changes that grant write tokens or run untrusted PR code are a supply-chain risk",
                            "security",
                        )
                    )
        for line, text in change.added:
            secret = redact(text)
            if secret.count:
                out.append(
                    Finding(
                        "HIGH",
                        "possible secret added",
                        path,
                        line,
                        "[redacted]",
                        f"line matches {', '.join(secret.rules)}; move it to the environment or a secret store",
                        "security",
                    )
                )
                continue
            for pattern, severity, title, why in RISKY:
                if pattern.search(text) and not is_test:
                    out.append(Finding(severity, title, path, line, text, why, "security"))
            if DEBUG.search(text) and not is_test:
                out.append(
                    Finding(
                        "LOW",
                        "debug output left in",
                        path,
                        line,
                        text,
                        "remove before merging or use the project's logger",
                        "observability",
                    )
                )
            if TODO.search(text):
                out.append(
                    Finding(
                        "NIT",
                        "new TODO",
                        path,
                        line,
                        text,
                        "track it in an issue or resolve it",
                        "maintainability",
                    )
                )
    if source_changed and not tests_changed:
        out.append(
            Finding(
                "LOW",
                "source changed without test changes",
                None,
                None,
                "",
                "add or update tests for the changed behaviour, or say why none are needed",
                "tests",
            )
        )
    return out


# ---------------------------------------------------------------- model pass


class ModelFinding(BaseModel):
    severity: Literal["BLOCKER", "HIGH", "MEDIUM", "LOW", "NIT"]
    file: str
    line: int = Field(description="Line number in the new version of the file")
    title: str
    evidence: str = Field(description="An exact line copied from the diff")
    reasoning: str
    category: str = Field(
        description="correctness, security, concurrency, performance, error handling, compatibility, migration, architecture, tests"
    )


class ModelFindings(BaseModel):
    findings: list[ModelFinding]


REVIEW_INSTRUCTIONS = (
    "You review a code diff. Report only real problems you can point to: correctness, regressions, "
    "security, authorization, concurrency, performance, error handling, observability, "
    "compatibility, migration safety, architecture, tests. Every finding must quote an exact line "
    "from the diff as evidence and give the new-file line number. If there are no problems, return "
    "an empty list. Do not invent findings. " + DATA_RULE
)


def verify_finding(f: ModelFinding, changes: dict[str, FileChange], repo: Path | None) -> bool:
    change = changes.get(f.file)
    evidence = f.evidence.strip()
    if change is None or not evidence:
        return False
    in_diff = any(
        evidence in text or (text.strip() in evidence and len(text.strip()) > 8)
        for _, text in change.added
    ) or any(evidence in r for r in change.removed)
    if not in_diff:
        return False
    if repo is not None and (repo / f.file).is_file():
        lines = (repo / f.file).read_text(errors="replace").splitlines()
        if not 1 <= f.line <= len(lines):
            return False
        window = "\n".join(lines[max(0, f.line - 4) : f.line + 3])
        if evidence not in window and not any(r for r in change.removed if evidence in r):
            return False
    return True


async def model_findings(
    model: Model,
    changes: list[FileChange],
    diff_text: str,
    repo: Path | None,
    max_chars: int,
    instructions: str = REVIEW_INSTRUCTIONS,
) -> tuple[list[Finding], int]:
    by_path = {c.path: c for c in changes}
    chunks: list[str] = []
    current = ""
    for block in re.split(r"(?=^diff --git )", diff_text, flags=re.M):
        if len(current) + len(block) > max_chars and current:
            chunks.append(current)
            current = ""
        current += block[:max_chars]
    if current:
        chunks.append(current)
    kept: list[Finding] = []
    dropped = 0
    for chunk in chunks:
        try:
            result = await structured(
                model,
                GenerateRequest.prompt(
                    untrusted_block("diff", redact_diff(chunk)),  # secrets never reach a model
                    instructions=instructions,
                    purpose="code",
                    timeout_s=300,
                ),
                ModelFindings,
            )
        except ModelError:
            continue
        for f in result.findings:
            if verify_finding(f, by_path, repo):
                kept.append(
                    Finding(
                        f.severity,
                        f.title,
                        f.file,
                        f.line,
                        f.evidence,
                        f.reasoning,
                        f.category,
                        source="model",
                    )
                )
            else:
                dropped += 1
    return kept, dropped


# ---------------------------------------------------------------- entry point


async def review(
    rt: Runtime | None,
    diff_text: str,
    *,
    repo: Path | None = None,
    base: str = "HEAD",
    model: Model | None = None,
) -> ReviewReport:
    repo = repo or (rt.repo if rt else None)
    changes = parse_diff(diff_text)
    findings = deterministic_findings(changes, repo)
    report = ReviewReport(
        findings,
        [c.path for c in changes],
        sum(len(c.added) for c in changes),
        sum(len(c.removed) for c in changes),
    )
    if rt is not None:
        rt.event("user_request", text="review the diff")
        intel = await gather(rt.repo, rt.canon, rt.graph)
        if intel.graph is not None:
            before = await graph_at_ref(rt.repo, base)
            after = await rt.graph.refresh(rt.repo)
            if before is not None and after is not None:
                delta = structural_diff(before, after, roles_from_canon(intel.canon))
                if not delta.empty:
                    report.architecture = delta.markdown()
                for warning in delta.boundary_warnings:
                    report.findings.append(
                        Finding(
                            "MEDIUM",
                            "architecture boundary crossed",
                            None,
                            None,
                            warning.edge,
                            warning.reason,
                            "architecture",
                        )
                    )
        else:
            report.notes.append("RepoKG unavailable: structural impact not analyzed")
        if model is None:
            try:
                model = rt.pool.coder("primary")
            except ModelError:
                model = None
    if model is not None and changes:
        # characters per diff chunk: the Apple FM helper fits ~8k tokens in all (plan.py),
        # local coders far more
        budget = 6000 if getattr(model, "name", "") == "helper" else 16000
        kept, dropped = await model_findings(model, changes, diff_text, repo, budget)
        report.findings += kept
        report.dropped = dropped
    elif changes:
        report.notes.append(
            "no local coding model: only deterministic checks ran (correctness was not reviewed)"
        )
    if rt is not None:
        rt.event(
            "verification",
            check="review",
            ok=not any(f.severity in {"BLOCKER", "HIGH"} for f in report.findings),
            findings=len(report.findings),
            dropped=report.dropped,
        )
    return report
