"""Secret detection and redaction.

Rules mirror Reflex's `packages/safety/src/redact.ts` (same rule names; a parity
test checks the sibling checkout) plus DFM-specific ones for keys on command
lines, seed phrases, kubeconfig key data and cookies.

Two profiles:
- CONTEXT: what a model may see. Secrets only. Emails, home paths and ordinary
  `KEY=value` lines stay, because rewriting them would corrupt code the model edits.
- TRACE: what DFM stores and hands to Reflex. Everything Reflex redacts.

Prefer false positives over leaks. Every replacement contains `[REDACTED`,
`[EMAIL]` or `<user>`, so downstream code can refuse to write redacted text back
into files (see `contains_redaction`).
"""

import getpass
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Any, cast

Replace = str | Callable[[re.Match[str]], str]


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    replace: Replace
    profiles: frozenset[str]


class Profile(StrEnum):
    CONTEXT = "context"
    TRACE = "trace"


_BOTH = frozenset({Profile.CONTEXT, Profile.TRACE})
_TRACE = frozenset({Profile.TRACE})

_SENSITIVE_KEY = (
    r"[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|PASS|URL|URI|DSN|CREDENTIALS?"
    r"|AUTH|COOKIE|SESSION|MNEMONIC|SEED|PHRASE|PRIVATE)[A-Z0-9_]*"
)


def _r(
    name: str, pattern: str, replace: Replace, profiles: frozenset[str] = _BOTH, flags: int = 0
) -> _Rule:
    return _Rule(name, re.compile(pattern, flags), replace, profiles)


_RULES: list[_Rule] = [
    _r(
        "private-key",
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
        r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
        "[REDACTED PRIVATE KEY]",
    ),
    _r(
        "sensitive-assignment",
        rf"\b({_SENSITIVE_KEY})(\s*[=:]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;]+)",
        lambda m: f"{m.group(1)}{': ' if ':' in m.group(2) else '='}[REDACTED]",
    ),
    _r(
        "aws-configure",
        r"\b(aws_secret_access_key|aws_session_token|aws_access_key_id)\s+(?!\[REDACTED)[^\s\"']+",
        lambda m: f"{m.group(1)} [REDACTED]",
        flags=re.I,
    ),
    _r(
        "credential-field",
        r"([\"']?)\b(password|passwd|pwd|pass|secret|token|api[_-]?key|apikey|access[_-]?token"
        r"|refresh[_-]?token|id[_-]?token|client[_-]?secret|auth[_-]?token|private[_-]?key"
        r"|mnemonic|seed[_-]?phrase|client-key-data)\b\1(\s*[=:]\s*)"
        r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s&,;\"'}]+)",
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}[REDACTED]",
        flags=re.I,
    ),
    _r(
        "cli-secret-flag",
        r"(--(?:private-key|mnemonic|password|passphrase|token|api-key|secret|auth-token)"
        r"(?:=|\s+))(?!\[REDACTED)(\"[^\"]*\"|'[^']*'|\S+)",
        lambda m: f"{m.group(1)}[REDACTED]",
    ),
    _r(
        "authorization-header",
        r"\b(authorization\s*:\s*)(bearer|basic|token|bot)\s+[^\s\"']+",
        lambda m: f"{m.group(1)}{m.group(2)} [REDACTED]",
        flags=re.I,
    ),
    _r(
        "api-key-header",
        r"\b((?:x-api-key|api-key|x-auth-token|private-token)\s*:\s*)[^\s\"']+",
        lambda m: f"{m.group(1)}[REDACTED]",
        flags=re.I,
    ),
    _r(
        "cookie-header",
        r"\b((?:set-)?cookie\s*:\s*)[^\n]+",
        lambda m: f"{m.group(1)}[REDACTED]",
        flags=re.I,
    ),
    _r(
        "bearer",
        r"\b(bearer)\s+[A-Za-z0-9._~+/-]{8,}=*",
        lambda m: f"{m.group(1)} [REDACTED]",
        flags=re.I,
    ),
    _r(
        "connection-string",
        r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqps?|mssql|sqlserver"
        r"|clickhouse)://[^\s\"'`<>]+",
        "[REDACTED_CONNECTION_STRING]",
        flags=re.I,
    ),
    _r(
        "url-credentials",
        r"\b([a-z][a-z0-9+.-]*://)[^\s:/@\"']+:[^\s@\"']+@",
        lambda m: f"{m.group(1)}[REDACTED]@",
        flags=re.I,
    ),
    _r("anthropic-key", r"\bsk-ant-[A-Za-z0-9_-]{16,}", "[REDACTED_API_KEY]"),
    _r("openai-key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}", "[REDACTED_API_KEY]"),
    _r("aws-access-key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b", "[REDACTED_AWS_KEY]"),
    _r(
        "github-token",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})",
        "[REDACTED_TOKEN]",
    ),
    _r("gitlab-token", r"\bglpat-[A-Za-z0-9_-]{20,}", "[REDACTED_TOKEN]"),
    _r("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}", "[REDACTED_TOKEN]"),
    _r("google-key", r"\bAIza[0-9A-Za-z_-]{35}\b", "[REDACTED_API_KEY]"),
    _r("google-oauth", r"\bya29\.[0-9A-Za-z_-]{20,}", "[REDACTED_TOKEN]"),
    _r("stripe-key", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}", "[REDACTED_API_KEY]"),
    _r("npm-token", r"\bnpm_[A-Za-z0-9]{36}\b", "[REDACTED_TOKEN]"),
    _r("hf-token", r"\bhf_[A-Za-z0-9]{30,}\b", "[REDACTED_TOKEN]"),
    _r(
        "jwt",
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        "[REDACTED_JWT]",
    ),
    _r(
        "seed-phrase",
        # A whole quoted or assigned value of 12-24 short lowercase words (BIP-39 shaped).
        r"(?m)([\"'=:]\s*|^\s*)((?:[a-z]{3,8}[ \t]+){11,23}[a-z]{3,8})(?=\s*[\"',]|\s*$)",
        lambda m: (
            m.group(0)
            if len(m.group(2).split()) not in {12, 15, 18, 21, 24}
            else f"{m.group(1)}[REDACTED_SEED_PHRASE]"
        ),
    ),
    _r(
        "dotenv-line",
        r"^(\s*(?:export\s+)?[A-Z][A-Z0-9_]{1,})=(?!\[REDACTED)(\S+)\s*$",
        lambda m: f"{m.group(1)}=[REDACTED]",
        _TRACE,
        re.M,
    ),
    _r(
        "email",
        r"\b(?!git@)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",
        "[EMAIL]",
        _TRACE,
    ),
    _r("macos-home", r"(?<![\w.-])/Users/(?!Shared\b)[^/\s\"'`:]+", "~", _TRACE),
    _r("linux-home", r"(?<![\w.-])/home/[^/\s\"'`:]+", "~", _TRACE),
    _r("windows-home", r"\b[A-Za-z]:\\Users\\[^\\\s\"'`]+", "~", _TRACE),
    _r("encoded-home", r"-(?:Users|home)-[A-Za-z0-9._]+(?=-|\b)", "-~", _TRACE),
    _r(
        "high-entropy",
        r"\b(?=[A-Za-z0-9+_=-]*[A-Z])(?=[A-Za-z0-9+_=-]*[a-z])(?=[A-Za-z0-9+_=-]*[0-9])"
        r"[A-Za-z0-9+_=-]{40,}\b",
        "[REDACTED_TOKEN]",
    ),
]

# Login names that are everyday words; for these only the home folder is redacted (as in Reflex).
_COMMON_NAMES = {
    *"root admin administrator user guest test tester dev developer build builder".split(),
    *"runner deploy app node git www web server ubuntu debian centos fedora".split(),
    *"alpine vagrant docker jenkins codespace vscode coder gitpod default owner".split(),
    *"local home work ops operator service bot agent demo sandbox main master".split(),
    *"will mark max bill rob pat sue may june april august art grant".split(),
    *"hunter chase rose frank gene ray jack joy hope grace dawn summer guy".split(),
}


def _user_rules() -> list[_Rule]:
    rules: list[_Rule] = []
    home = str(Path.home()).rstrip("/")
    if len(home) >= 2:
        rules.append(_r("home", rf"(?<![\w.-]){re.escape(home)}(?![\w.-])", "~", _TRACE))
    try:
        name = getpass.getuser()
    except (KeyError, OSError):
        name = ""
    if len(name) >= 3 and name.lower() not in _COMMON_NAMES:
        rules.append(_r("username", rf"\b{re.escape(name)}\b", "<user>", _TRACE, re.I))
    return rules


_ALL_RULES = _RULES + _user_rules()
RULE_NAMES = frozenset(rule.name for rule in _ALL_RULES)

_MARKER = re.compile(r"\[REDACTED[^\]]*\]|\[EMAIL\]|<user>")


@dataclass(frozen=True)
class RedactionResult:
    text: str
    count: int
    rules: tuple[str, ...]


def redact(text: str, profile: Profile = Profile.CONTEXT) -> RedactionResult:
    count = 0
    fired: dict[str, None] = {}
    for rule in _ALL_RULES:
        if profile not in rule.profiles:
            continue

        def sub(match: re.Match[str], rule: _Rule = rule) -> str:
            nonlocal count
            count += 1
            fired[rule.name] = None
            return rule.replace if isinstance(rule.replace, str) else rule.replace(match)

        text = rule.pattern.sub(sub, text)
    return RedactionResult(text, count, tuple(fired))


def redact_text(text: str, profile: Profile = Profile.CONTEXT) -> str:
    return redact(text, profile).text


_SECRET_KEY_NAME = re.compile(_SENSITIVE_KEY, re.I)
_SECRET_FLAG = re.compile(
    r"^--?(?:private-keys?|mnemonics?|password|passphrase|token|api-key|secret|auth-token|"
    r"access-key|secret-key|keystore-password|unlock-password|seed|seed-phrase)$"
)


# flags whose value is a password for one program only (`-p` is a port elsewhere)
_PROGRAM_SECRET_FLAGS = {
    "redis-cli": frozenset({"-a", "--pass"}),
    "openssl": frozenset({"-pass", "-passin", "-passout", "-k"}),
    "sshpass": frozenset({"-p"}),
    "mysql": frozenset({"--password"}),
}
_MYSQL_FAMILY = frozenset({"mysql", "mysqldump", "mysqladmin", "mariadb", "mariadb-dump"})
_SECRET_FLAG_VALUE = re.compile(r"^(--?(?:password|passwd|pass|token|api-key|secret)=).+", re.I)


def redact_argv(argv: list[str], profile: Profile = Profile.TRACE) -> list[str]:
    """Redact an argument vector, including values that follow a secret flag."""
    program = PurePath(argv[0]).name if argv else ""
    secret_flags = _PROGRAM_SECRET_FLAGS.get(program, frozenset())
    out: list[str] = []
    hide_next = False
    for arg in argv:
        if hide_next:
            out.append("[REDACTED]")
        elif program in _MYSQL_FAMILY and arg.startswith("-p") and len(arg) > 2:
            out.append("-p[REDACTED]")  # mysql -pSECRET: the password is attached
        elif match := _SECRET_FLAG_VALUE.match(arg):
            out.append(match.group(1) + "[REDACTED]")
        elif program in {"curl", "wget"} and ":" in arg and out and out[-1] in {"-u", "--user"}:
            out.append(arg.split(":", 1)[0] + ":[REDACTED]")  # curl -u user:password
        else:
            out.append(redact_text(arg, profile))
        hide_next = bool(_SECRET_FLAG.match(arg)) or arg in secret_flags
    return out


def redact_deep(value: Any, profile: Profile = Profile.TRACE) -> Any:
    """Redact every string inside a JSON-like value; string lists are treated as argv."""
    if isinstance(value, str):
        return redact_text(value, profile)
    if isinstance(value, list | tuple):
        items: list[Any] = [*cast(list[Any], value)]
        if all(isinstance(v, str) for v in items):
            return redact_argv(cast(list[str], items), profile)
        return [redact_deep(v, profile) for v in items]
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if isinstance(v, str) and _SECRET_KEY_NAME.fullmatch(k)
            else redact_deep(v, profile)
            for k, v in cast(dict[str, Any], value).items()
        }
    return value


def contains_redaction(text: str) -> bool:
    """True if text carries a redaction marker; such text must never be written into a file."""
    return _MARKER.search(text) is not None


# ---------------------------------------------------------------- secret-bearing files

_SECRET_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "kubeconfig",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    ".htpasswd",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "secring.gpg",
}
_SECRET_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".tfvars",
    ".tfstate",
    ".kdbx",
    ".gpg",
    ".asc",
    ".ppk",
)
_SECRET_PREFIXES = (
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".env.",
    "service-account",
    "secrets.",
)
_SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube", "keystores", ".starkli-wallets", ".docker"}
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def is_secret_path(path: str | PurePath) -> bool:
    p = PurePath(path)
    name = p.name.lower()
    if name.endswith(".pub"):
        return False
    if (
        name in _SECRET_NAMES
        or name.endswith(_SECRET_SUFFIXES)
        or name.startswith(_SECRET_PREFIXES)
    ):
        return True
    if name == "config" and p.parent.name in {".kube", ".docker", ".aws", ".ssh"}:
        return True
    return any(part in _SECRET_DIRS for part in p.parts[:-1])


_KV_LINE = re.compile(r"^(\s*(?:export\s+)?[\w.\-\[\]\"']+\s*[=:]\s*)(.+?)\s*$")


def redact_secret_file(path: str | PurePath, text: str) -> str:
    """What a model may see of a secret-bearing file: keys and structure, never values.

    Templates (`.env.example`) still get values redacted: placeholders are cheap to lose,
    real values committed by mistake are not.
    """
    name = PurePath(path).name.lower()
    looks_kv = name.startswith(".env") or name.endswith((".tfvars", ".npmrc", ".pypirc", ".netrc"))
    if not looks_kv and not name.endswith(_TEMPLATE_SUFFIXES):
        return f"[REDACTED: secret file {PurePath(path).name}, {len(text.splitlines())} lines]"
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            out.append(line)
            continue
        match = _KV_LINE.match(line)
        out.append(f"{match.group(1)}[REDACTED]" if match else "[REDACTED]")
    return "\n".join(out)


_DIFF_HEADER = (
    "diff --git",
    "index ",
    "--- ",
    "+++ ",
    "@@",
    "new file",
    "deleted file",
    "similarity",
    "rename ",
    "old mode",
    "new mode",
    "Binary files",
)


def redact_diff(diff: str, profile: Profile = Profile.CONTEXT) -> str:
    """A unified diff as a model may see it: secret-bearing files keep keys and structure, never
    values (as redact_secret_file does for reads); every other file gets `profile` redaction."""
    out: list[str] = []
    for block in re.split(r"(?=^diff --git )", diff, flags=re.M):
        header = re.match(r"diff --git a/(\S+) b/(\S+)", block)
        path = header.group(2) if header else None
        if path is not None and is_secret_path(path):
            lines: list[str] = []
            for line in block.splitlines():
                if line.startswith(_DIFF_HEADER) or not line[1:].strip() or line[:1] not in "+- ":
                    lines.append(line)
                else:
                    lines.append(line[0] + redact_secret_file(path, line[1:]).splitlines()[0])
            block = "\n".join(lines) + ("\n" if block.endswith("\n") else "")
        out.append(redact(block, profile).text)
    return "".join(out)
