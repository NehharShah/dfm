"""Classify shell commands before they run.

The command is tokenized the way a POSIX shell would (so quote tricks like
`r''m` classify as `rm`), split into simple commands at `; && || | &` and
newlines, unwrapped (`env`, `timeout`, `sh -c`, `uv run`, `xargs`, ...) and
matched against per-tool rules in `dfm.tools.shell.rules`. A compound command
is as dangerous as its most dangerous part. Anything unrecognized is UNKNOWN,
which policy treats as "ask", never as safe.

Path arguments that leave the repository (absolute, `~`, `..`) are checked with
the caller's `inside` predicate; with no predicate they count as outside.
"""

import dataclasses
import os
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from dfm.policy.types import (
    SEVERITY,
    ActionClass,
    NetworkCategory,
    ProposedAction,
    most_severe,
)
from dfm.secrets import redact_argv, redact_text
from dfm.tools.blockchain.chains import chain_target

A = ActionClass
_CHAIN_NETWORKS = {NetworkCategory.BLOCKCHAIN_RPC_READ, NetworkCategory.BLOCKCHAIN_RPC_WRITE}

_OPERATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", "|&", ";;"}
_REDIRECTS = {">", ">>", "<", ">|", "&>", "&>>", ">&", "<&", "<>"}
SHELL_TOKENS = frozenset(_OPERATORS | _REDIRECTS)
# $HOME, ${HOME}: not a regex anchor such as '^TestAdd$'
_SHELL_VAR = re.compile(r"\$(\{|[A-Za-z_])")


def shell_tokens(command: str | Sequence[str]) -> list[str]:
    """The tokens a shell would see: operators and redirects split out of a command string."""
    return _tokenize(command) if isinstance(command, str) else list(command)


_INTERPRETERS = {"sh", "bash", "zsh", "dash", "fish", "python", "python3", "node", "perl", "ruby"}
_SAFE_DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/tty"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# `VAR=value cmd` prefixes that cannot change which program runs or what it loads. Anything
# else (PATH, IFS, GIT_*, *_OPTIONS, *_CONFIG_PATH, LD_*/DYLD_*, PYTHON*, ...) can, so a
# command with such a prefix is UNKNOWN and the developer sees the full line before it runs.
_BENIGN_ASSIGNMENTS = frozenset(
    {
        "CI",
        "NO_COLOR",
        "FORCE_COLOR",
        "CLICOLOR",
        "CLICOLOR_FORCE",
        "TERM",
        "COLUMNS",
        "LINES",
        "TZ",
        "LANG",
        "LANGUAGE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONHASHSEED",
        "RUST_BACKTRACE",
        "RUST_LOG",
        "CARGO_TERM_COLOR",
        "NODE_ENV",
        "DEBUG",
        "LOG_LEVEL",
        "VERBOSE",
    }
)
_BENIGN_ASSIGNMENT_PREFIXES = ("LC_",)
# A shell expands these before the program sees them; the classifier would be reading a
# different argument than the one that runs.
_SHELL_EXPANSIONS = (
    ("`", "command substitution"),
    ("$(", "command substitution"),
    ("<(", "process substitution"),
    (">(", "process substitution"),
    ("$'", "ANSI-C quoting"),
    ('$"', "locale quoting"),
)


def _brace_expansion(command: str) -> bool:
    """`{a,b}` or `{1..3}` outside quotes: the shell expands it into other arguments."""
    quote: str | None = None
    depth = 0
    body = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"':
                i += 1
        elif ch == "\\":
            i += 1
        elif ch in {"'", '"'}:
            quote = ch
        elif ch == "{":
            depth += 1
            body = ""
        elif ch == "}" and depth:
            depth -= 1
            if "," in body or ".." in body:
                return True
        elif depth:
            body += ch
        i += 1
    return False


def _benign_assignment(assignment: str) -> bool:
    name = assignment.split("=", 1)[0]
    return name in _BENIGN_ASSIGNMENTS or name.startswith(_BENIGN_ASSIGNMENT_PREFIXES)


@dataclass(frozen=True)
class Part:
    argv: tuple[str, ...]
    action: ActionClass
    network: NetworkCategory | None = None
    reasons: tuple[str, ...] = ()
    target: str | None = None


@dataclass(frozen=True)
class Classification:
    command: str
    parts: tuple[Part, ...]
    outside_boundary: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default=())

    @property
    def action(self) -> ActionClass:
        return most_severe([p.action for p in self.parts])

    @property
    def network(self) -> NetworkCategory | None:
        ranked = sorted(self.parts, key=lambda p: SEVERITY[p.action], reverse=True)
        return next((p.network for p in ranked if p.network is not None), None)

    def to_actions(self, proposed_by: str) -> list[ProposedAction]:
        """One ProposedAction per part; the command runs only if every one is allowed."""
        inside = not self.outside_boundary
        whole = redact_text(self.command)
        return [
            ProposedAction(
                kind="shell",
                action_class=part.action,
                # what the approver sees and the trace stores: never key material or tokens
                summary=" ".join(shlex.quote(a) for a in redact_argv(list(part.argv))) or whole,
                proposed_by=proposed_by,
                target=part.target,
                network=part.network,
                inside_boundary=inside,
                reasons=part.reasons + self.reasons,
                command=whole if len(self.parts) > 1 else None,
            )
            for part in self.parts
        ]


InsidePredicate = Callable[[str], bool]


def classify(
    command: str | Sequence[str], *, inside: InsidePredicate | None = None
) -> Classification:
    text = command if isinstance(command, str) else shlex.join(command)
    reasons: list[str] = []
    if isinstance(command, str):
        try:
            tokens = _tokenize(command)
        except ValueError as exc:
            return _unknown(text, f"cannot parse: {exc}")
        for marker, why in _SHELL_EXPANSIONS:
            if marker in command:
                reasons.append(why)
        if _brace_expansion(command):
            reasons.append("brace expansion")
    else:
        tokens = list(command)

    if "<<" in tokens or "<<<" in tokens or "<<-" in tokens:
        return _unknown(text, "here-document")

    outside: list[str] = []
    parts: list[Part] = []
    segments = _segments(tokens)
    for index, (argv, redirects, reads, _) in enumerate(segments):
        for path in (*redirects, *reads):
            if path not in _SAFE_DEVICES and not _is_inside(path, inside):
                outside.append(path)
        if not argv:
            continue
        pipes_into_interpreter = index > 0 and segments[index - 1][3] in {"|", "|&"}
        for part in _classify_simple(argv, inside, outside):
            if pipes_into_interpreter and _basename(part.argv[0]) in _INTERPRETERS:
                part = Part(
                    part.argv,
                    A.DESTRUCTIVE,
                    part.network,
                    (*part.reasons, "pipes into an interpreter"),
                )
            parts.append(part)
        if any(p not in _SAFE_DEVICES for p in redirects):
            parts.append(Part(tuple(argv), A.WORKTREE_WRITE, None, ("redirects output to a file",)))

    if reasons:
        parts = [_at_least(p, A.UNKNOWN, reasons) for p in parts] or [
            Part((), A.UNKNOWN, None, tuple(reasons))
        ]
    if not parts:
        return _unknown(text, "empty command")
    return Classification(text, tuple(parts), tuple(dict.fromkeys(outside)), tuple(reasons))


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


Segment = tuple[list[str], list[str], list[str], str | None]


def _segments(tokens: list[str]) -> list[Segment]:
    """Simple commands as (argv, files written, files read, operator that ended it)."""
    out: list[Segment] = []
    argv: list[str] = []
    writes: list[str] = []
    reads: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _OPERATORS:
            out.append((argv, writes, reads, tok))
            argv, writes, reads = [], [], []
        elif tok in _REDIRECTS:
            if argv and argv[-1].isdigit():
                argv.pop()  # fd number, as in 2>
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 1
            if tok in {">&", "<&"} and (target.isdigit() or target == "-"):
                pass  # fd duplication, not a file
            elif tok in {"<", "<&"}:
                reads.append(target)
            else:
                writes.append(target)
        else:
            argv.append(tok)
        i += 1
    out.append((argv, writes, reads, None))
    return out


def _basename(program: str) -> str:
    return program.rsplit("/", 1)[-1]


# Root-owned Apple toolchain installs: passing one of these as an argument (a compiler
# search path) reads the toolchain, nothing else. Normalized first, so `..` cannot escape.
# Xcode is often installed under a versioned name (Xcode_16.4.app, Xcode-beta.app): CI
# runners and developers with several Xcodes resolve `xcrun --find` to one of those.
_TOOLCHAIN_PREFIXES = ("/Library/Developer/CommandLineTools/",)
_XCODE_APP = re.compile(r"^/Applications/Xcode[^/]*\.app/")


def is_toolchain_path(path: str) -> bool:
    """`path` is inside a root-owned Apple toolchain install."""
    normalized = os.path.normpath(path)
    return normalized.startswith(_TOOLCHAIN_PREFIXES) or _XCODE_APP.match(normalized) is not None


def _leaves_cwd(value: str) -> bool:
    """`..`, `../x`, `./..`, `x/../..`: a relative path that climbs out of the working directory."""
    normalized = os.path.normpath(value)
    return normalized == ".." or normalized.startswith("../") or "/../" in value


def _is_inside(path: str, inside: InsidePredicate | None) -> bool:
    if _SHELL_VAR.search(path):
        return False
    if is_toolchain_path(path):
        return True
    if inside is not None:
        return inside(path)
    return not (path.startswith(("/", "~")) or _leaves_cwd(path))


def _path_args(argv: Sequence[str]) -> list[str]:
    paths: list[str] = []
    for arg in argv[1:]:
        value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
        if value.startswith("-") or "://" in value:
            continue
        if "," in value and "=" in value:  # docker --mount type=bind,src=/,dst=/h
            for piece in value.split(","):
                key, _, path = piece.partition("=")
                if key in {"src", "source"} and path.startswith(("/", "~", "..")):
                    paths.append(path)
            continue
        if ":" in value and value.startswith(("/", "~")):  # -v SRC:DST[:opts]: the host side
            value = value.split(":", 1)[0]
        # $HOME/x, ${HOME}: a shell decides where it points, not the repo (a `bash -c` script,
        # with spaces, is classified on its own)
        if _SHELL_VAR.search(value) and not any(c.isspace() for c in value):
            paths.append(value)
            continue
        if value.startswith(("/", "~")) or _leaves_cwd(value):
            paths.append(value)
    return paths


def _at_least(part: Part, floor: ActionClass, reasons: list[str]) -> Part:
    action = most_severe([part.action, floor])
    return Part(part.argv, action, part.network, (*part.reasons, *reasons), part.target)


def _unknown(text: str, reason: str) -> Classification:
    return Classification(text, (Part((), A.UNKNOWN, None, (reason,)),), (), (reason,))


def _classify_simple(
    argv: list[str], inside: InsidePredicate | None, outside: list[str]
) -> list[Part]:
    assignments: list[str] = []
    while argv and _ASSIGNMENT.match(argv[0]):
        assignments.append(argv[0])
        argv = argv[1:]
    # the value may point outside the repository (GIT_DIR=/other/.git, KUBECONFIG=~/x)
    for path in _path_args(["", *(a.split("=", 1)[1] for a in assignments)]):
        if path not in _SAFE_DEVICES and not _is_inside(path, inside):
            outside.append(path)
    if not argv:
        return [Part(tuple(assignments), A.SAFE_LOCAL, None, ("variable assignment",))]
    risky = [a for a in assignments if not _benign_assignment(a)]
    if risky:
        names = ", ".join(a.split("=", 1)[0] for a in risky)
        return [
            Part(
                (*assignments, *argv),
                A.UNKNOWN,
                None,
                (f"environment prefix may change what runs: {names}",),
            )
        ]
    parts = _classify_program(argv, inside, outside)
    if not assignments:
        return parts
    # the approver and the trace see the whole simple command, prefixes included
    return [dataclasses.replace(p, argv=(*assignments, *p.argv)) for p in parts]


def _classify_program(
    argv: list[str], inside: InsidePredicate | None, outside: list[str]
) -> list[Part]:
    from dfm.tools.shell import rules

    for path in _path_args(argv):
        if path not in _SAFE_DEVICES and not _is_inside(path, inside):
            outside.append(path)
    program = argv[0]
    if "$" in program:
        return [Part(tuple(argv), A.UNKNOWN, None, ("command name comes from a variable",))]
    name = _basename(program)
    trusted_path = (
        name in rules.PATH_ANYWHERE
        or os.path.normpath(program).startswith(rules.SYSTEM_BIN_PREFIXES)
        or is_toolchain_path(program)
    )
    if "/" in program and not (name in rules.PATH_OK and trusted_path):
        # `./tools/cat` may be anything the model just wrote: a read-only name proves nothing
        return [
            Part(tuple(argv), A.UNKNOWN, None, (f"runs a script or binary by path: {program}",))
        ]

    unwrapped = rules.unwrap(name, argv)
    if unwrapped is not None:
        kind, inner = unwrapped
        if kind == "string":
            nested = classify(inner[0], inside=inside)
            outside.extend(nested.outside_boundary)
            return list(nested.parts)
        if not inner:
            return [Part(tuple(argv), A.UNKNOWN, None, (f"{name} with nothing to run",))]
        return _classify_simple(list(inner), inside, outside)

    rule = rules.RULES.get(name)
    if rule is None:
        return [Part(tuple(argv), A.UNKNOWN, None, (f"no rule for '{name}'",))]
    part = rule([name, *argv[1:]])
    if part.network in _CHAIN_NETWORKS:
        # local / testnet / mainnet:<name>; an unrecognised endpoint counts as mainnet
        part = dataclasses.replace(part, target=chain_target(argv).label)
    return [part]
