"""Per-tool classification rules. Each rule sees argv with argv[0] = program basename.

When in doubt a rule returns UNKNOWN or the more dangerous class. Docker,
Kubernetes and blockchain targets are refined by `dfm/tools/{docker,kubernetes,blockchain}`.
"""

import re
import shlex
from collections.abc import Callable, Sequence

from dfm.policy.network import categorize, host_of, is_loopback
from dfm.policy.types import KEY_MATERIAL, ActionClass, NetworkCategory
from dfm.tools.shell.classify import Part

A = ActionClass
N = NetworkCategory
Rule = Callable[[list[str]], Part]


def P(
    argv: Sequence[str],
    action: ActionClass,
    reason: str,
    network: NetworkCategory | None = None,
    target: str | None = None,
) -> Part:
    return Part(tuple(argv), action, network, (reason,), target)


def positional(argv: Sequence[str], with_value: frozenset[str] = frozenset()) -> list[str]:
    """Non-option arguments after argv[0], skipping options that take a value."""
    out: list[str] = []
    skip = False
    for arg in argv[1:]:
        if skip:
            skip = False
            continue
        if arg == "--":
            continue
        if arg.startswith("-"):
            if arg in with_value:
                skip = True
            continue
        out.append(arg)
    return out


def has(argv: Sequence[str], *flags: str) -> bool:
    for arg in argv[1:]:
        if arg in flags:
            return True
        if arg.startswith("--") and arg.split("=", 1)[0] in flags:
            return True
        # combined short flags: -rf contains -r and -f
        if (
            arg.startswith("-")
            and not arg.startswith("--")
            and len(arg) > 2
            and any(f"-{c}" in flags for c in arg[1:])
        ):
            return True
    return False


def option_values(argv: Sequence[str], *names: str) -> list[str]:
    values: list[str] = []
    for i, arg in enumerate(argv):
        for name in names:
            if arg == name and i + 1 < len(argv):
                values.append(argv[i + 1])
            elif arg.startswith(name + "="):
                values.append(arg.split("=", 1)[1])
    return values


def option_value(argv: Sequence[str], *names: str) -> str | None:
    values = option_values(argv, *names)
    return values[0] if values else None


# Programs that may be invoked by path (e.g. /usr/bin/git) and keep their rule: from a system
# location, or anywhere for interpreters whose rule already means "runs repository code".
PATH_OK = frozenset({"git", "python3", "python", "env", "rg", "grep", "ls", "cat", "uv", "fm"})
PATH_ANYWHERE = frozenset({"python3", "python", "uv"})
SYSTEM_BIN_PREFIXES = (
    "/usr/bin/",
    "/bin/",
    "/usr/sbin/",
    "/sbin/",
    "/usr/libexec/",
    "/usr/local/bin/",
    "/opt/homebrew/bin/",
    "/opt/homebrew/opt/",
    "/Library/Developer/CommandLineTools/",
    "/Applications/Xcode.app/",
)


# ---------------------------------------------------------------- wrappers

_UV_RUN_WITH_VALUE = frozenset(
    {"--with", "--python", "-p", "--directory", "--project", "--package", "--extra", "--group"}
)


def unwrap(name: str, argv: list[str]) -> tuple[str, list[str]] | None:
    """('argv', inner argv) or ('string', [script]) for wrappers; None otherwise."""
    rest = argv[1:]
    if name in {"time", "nohup", "command", "caffeinate", "stdbuf"}:
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 1
        return ("argv", rest[i:])
    if name == "env":
        i = 0
        while i < len(rest) and (rest[i].startswith("-") or "=" in rest[i]):
            split = rest[i] in {"-S", "--split-string"} or rest[i].startswith(
                ("-S", "--split-string=")
            )
            if split:  # env splits this string into the command it runs, plus what follows
                if rest[i] in {"-S", "--split-string"}:
                    head, after = (rest[i + 1] if i + 1 < len(rest) else ""), rest[i + 2 :]
                else:
                    head, after = (
                        rest[i].split("=", 1)[-1] if "=" in rest[i] else rest[i][2:],
                        rest[i + 1 :],
                    )
                return ("string", [" ".join([head, *(shlex.quote(a) for a in after)])])
            i += 2 if rest[i] in {"-u", "-C"} else 1
        return ("argv", rest[i:]) if i < len(rest) else None
    if name == "nice":
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] == "-n" else 1
        return ("argv", rest[i:])
    if name == "timeout":
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in {"-s", "-k", "--signal", "--kill-after"} else 1
        return ("argv", rest[i + 1 :])
    if name == "xargs":
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in {"-I", "-n", "-P", "-L", "-d", "-E", "-s"} else 1
        return ("argv", rest[i:] or ["echo"])
    if name in {"sh", "bash", "zsh", "dash"} and "-c" in rest:
        script = rest[rest.index("-c") + 1] if rest.index("-c") + 1 < len(rest) else ""
        return ("string", [script])
    if name in {"uv", "poetry", "pdm", "hatch", "pipenv"} and rest[:1] == ["run"]:
        inner = rest[1:]
        i = 0
        while i < len(inner) and inner[i].startswith("-"):
            i += 2 if inner[i] in _UV_RUN_WITH_VALUE else 1
        return ("argv", inner[i:])
    if name in {"pnpm", "bun", "yarn", "npm"} and rest[:1] == ["exec"]:
        inner = rest[1:]
        i = 0
        while i < len(inner) and inner[i].startswith("-"):
            i += 1
        return ("argv", inner[i:])
    return None


# ---------------------------------------------------------------- simple tools

SAFE_READ = {
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "fd",
    "tree",
    "pwd",
    "echo",
    "printf",
    "which",
    "type",
    "whereis",
    "stat",
    "file",
    "du",
    "df",
    "diff",
    "cmp",
    "sort",
    "uniq",
    "cut",
    "tr",
    "jq",
    "yq",
    "less",
    "more",
    "basename",
    "dirname",
    "realpath",
    "readlink",
    "date",
    "uname",
    "whoami",
    "id",
    "ps",
    "lsof",
    "sw_vers",
    "vm_stat",
    "true",
    "false",
    "test",
    "[",
    "column",
    "nl",
    "comm",
    "paste",
    "fold",
    "rev",
    "md5",
    "shasum",
    "sha256sum",
    "md5sum",
    "hexdump",
    "xxd",
    "od",
    "strings",
    "nproc",
    "hostname",
    "uptime",
    "free",
    "top",
    "memory_pressure",
    "lscpu",
    "tokei",
    "cloc",
    "sleep",
    "seq",
    "yes",
    "tac",
    "expr",
    "bc",
    "getconf",
    "locale",
    "printenv",
    "ldd",
    "otool",
    "nm",
    "objdump",
    "zcat",
    "bzcat",
    "xzcat",
    "cd",
    "pushd",
    "popd",
    "set",
    "export",
    "unset",
    "alias",
    "history",
    "jobs",
    "wait",
    "dirs",
    "hash",
    "ulimit",
}
SAFE_READ_DEV = {
    "rust-analyzer",
    "gopls",
    "pyright-langserver",
    "typescript-language-server",
    "sourcekit-lsp",
    "clangd",
    "fm",
}


def rule_safe_read(argv: list[str]) -> Part:
    return P(argv, A.SAFE_READ, f"{argv[0]} only reads")


def rule_find(argv: list[str]) -> Part:
    if has(argv, "-delete"):
        return P(argv, A.DESTRUCTIVE, "find -delete removes files recursively")
    for flag in ("-exec", "-execdir", "-ok", "-okdir"):
        if flag in argv:
            return P(argv, A.UNKNOWN, f"find {flag} runs commands")
    if has(argv, "-fprint", "-fprintf", "-fls"):
        return P(argv, A.WORKTREE_WRITE, "find writes a file")
    return P(argv, A.SAFE_READ, "find only reads")


# GNU sed runs commands with `e` (a command, or the s///e flag) and writes with w/W
_SED_EXEC_OR_WRITE = re.compile(
    r"(^|[;\n{}])\s*(\$|\d+(,\s*\d+)?|/[^/\n]*/)?\s*!?\s*[ewW](\s|$|;|})"
)
_SED_SUBST_FLAGS = re.compile(r"s(.)(?:\\.|(?!\1).)*\1(?:\\.|(?!\1).)*\1([a-zA-Z0-9]*)")


def rule_sed(argv: list[str]) -> Part:
    if has(argv, "-i", "--in-place") or any(a.startswith("-i") for a in argv[1:]):
        return P(argv, A.WORKTREE_WRITE, "sed -i edits files in place")
    if has(argv, "-f", "--file"):
        return P(argv, A.UNKNOWN, "sed runs a script file")
    scripts = option_values(argv, "-e", "--expression") or [
        a for a in argv[1:2] if not a.startswith("-")
    ]
    for script in scripts:
        flags = "".join(m.group(2) for m in _SED_SUBST_FLAGS.finditer(script))
        if _SED_EXEC_OR_WRITE.search(script) or any(f in flags for f in "ewW"):
            return P(argv, A.UNKNOWN, "sed script may write files or execute commands")
    return P(argv, A.SAFE_READ, "sed without -i only reads")


# print/printf piped to a command or redirected to a file, `cmd | getline`, system()
_AWK_EXEC_OR_WRITE = re.compile(r"\bprintf?\b[^;}\n]*?(>|\|)|\|\s*getline|\bsystem\s*\(")


def rule_awk(argv: list[str]) -> Part:
    if has(argv, "-f", "--file"):
        return P(argv, A.UNKNOWN, "awk runs a script file")
    if _AWK_EXEC_OR_WRITE.search(" ".join(argv[1:])):
        return P(argv, A.UNKNOWN, "awk script runs commands or writes files")
    return P(argv, A.SAFE_READ, "awk only reads")


def rule_write(argv: list[str]) -> Part:
    return P(argv, A.WORKTREE_WRITE, f"{argv[0]} writes files")


def rule_rm(argv: list[str]) -> Part:
    if has(argv, "-r", "-R", "--recursive"):
        return P(argv, A.DESTRUCTIVE, "recursive delete")
    return P(argv, A.LOCAL_DESTRUCTIVE, "deletes files")


def rule_destructive(argv: list[str]) -> Part:
    return P(argv, A.DESTRUCTIVE, f"{argv[0]} destroys data")


def rule_privileged(argv: list[str]) -> Part:
    return P(argv, A.PRIVILEGED, f"{argv[0]} changes privileges or system state")


def rule_unknown_exec(argv: list[str]) -> Part:
    return P(argv, A.UNKNOWN, f"{argv[0]} executes arbitrary code")


def rule_kill(argv: list[str]) -> Part:
    return P(argv, A.LOCAL_DESTRUCTIVE, "signals processes (DFM stops only its own)")


def rule_chmod(argv: list[str]) -> Part:
    if has(argv, "-R") and any(a in {"/", "~"} for a in argv):
        return P(argv, A.DESTRUCTIVE, "recursive permission change on a root")
    return P(argv, A.WORKTREE_WRITE, "changes file permissions")


_TAR_RUNS = (
    "--use-compress-program",
    "--to-command",
    "--checkpoint-action",
    "--info-script",
    "--new-volume-script",
    "--rsh-command",
    "--rmt-command",
)


def rule_tar(argv: list[str]) -> Part:
    if has(argv, *_TAR_RUNS, "-I", "-F"):
        return P(argv, A.UNKNOWN, "tar runs a program (compressor, command or script)")
    flags = argv[1] if len(argv) > 1 else ""
    if "x" in flags.lstrip("-") or has(argv, "-x", "--extract"):
        return P(argv, A.WORKTREE_WRITE, "extracts an archive")
    if "c" in flags.lstrip("-") or has(argv, "-c", "--create"):
        return P(argv, A.WORKTREE_WRITE, "creates an archive")
    return P(argv, A.SAFE_READ, "lists an archive")


def rule_rg(argv: list[str]) -> Part:
    if has(argv, "--pre"):
        return P(argv, A.UNKNOWN, "rg --pre runs a program on every file")
    return P(argv, A.SAFE_READ, "rg only reads")


def rule_fd(argv: list[str]) -> Part:
    if has(argv, "-x", "--exec", "-X", "--exec-batch"):
        return P(argv, A.UNKNOWN, "fd --exec runs a command per match")
    return P(argv, A.SAFE_READ, "fd only reads")


def rule_sort(argv: list[str]) -> Part:
    if has(argv, "--compress-program"):
        return P(argv, A.UNKNOWN, "sort --compress-program runs a program")
    if has(argv, "-o", "--output"):
        return P(argv, A.WORKTREE_WRITE, "sort -o writes a file")
    return P(argv, A.SAFE_READ, "sort only reads")


def rule_output_operand(argv: list[str]) -> Part:
    """uniq IN OUT, xxd IN OUT: a second operand is a file they write."""
    operands = [a for a in argv[1:] if not a.startswith("-")]
    if len(operands) >= 2:
        return P(argv, A.WORKTREE_WRITE, f"{argv[0]} writes its second operand")
    return P(argv, A.SAFE_READ, f"{argv[0]} only reads")


def rule_yq(argv: list[str]) -> Part:
    if has(argv, "-i", "--inplace"):
        return P(argv, A.WORKTREE_WRITE, "yq -i edits files in place")
    return P(argv, A.SAFE_READ, "yq only reads")


def rule_tree(argv: list[str]) -> Part:
    if has(argv, "-o"):
        return P(argv, A.WORKTREE_WRITE, "tree -o writes a file")
    return P(argv, A.SAFE_READ, "tree only reads")


def rule_pager(argv: list[str]) -> Part:
    if any(a.startswith("+") for a in argv[1:]):
        return P(argv, A.UNKNOWN, f"{argv[0]} +commands can run shell commands")
    return P(argv, A.SAFE_READ, f"{argv[0]} only reads")


def rule_crontab(argv: list[str]) -> Part:
    if has(argv, "-r"):
        return P(argv, A.DESTRUCTIVE, "removes the crontab")
    if has(argv, "-l"):
        return P(argv, A.SAFE_READ, "lists the crontab")
    return P(argv, A.LOCAL_STATE_CHANGE, "edits the crontab")


def rule_sysctl(argv: list[str]) -> Part:
    if has(argv, "-w") or any("=" in a for a in argv[1:]):
        return P(argv, A.PRIVILEGED, "sets kernel parameters")
    return P(argv, A.SAFE_READ, "reads kernel parameters")


def rule_make(argv: list[str]) -> Part:
    targets = positional(argv, frozenset({"-C", "-f", "-j", "--directory", "--file"}))
    targets = [t for t in targets if "=" not in t]
    risky = {"deploy", "publish", "release", "push", "upload"}
    if any(any(r in t for r in risky) for t in targets):
        return P(argv, A.REMOTE_WRITE, f"{argv[0]} target looks like a deploy/publish step")
    if any(t in {"install", "uninstall"} or "clean" in t for t in targets):
        return P(argv, A.LOCAL_STATE_CHANGE, f"{argv[0]} {' '.join(targets)} changes local state")
    return P(argv, A.SAFE_BUILD, f"{argv[0]} build/test target runs repository code")


def rule_open(argv: list[str]) -> Part:
    return P(argv, A.LOCAL_STATE_CHANGE, "launches an application")


# ---------------------------------------------------------------- git

_GIT_GLOBAL_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_GIT_READ = {
    "status",
    "diff",
    "log",
    "show",
    "blame",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "cat-file",
    "describe",
    "shortlog",
    "grep",
    "merge-base",
    "name-rev",
    "for-each-ref",
    "rev-list",
    "show-ref",
    "check-ignore",
    "var",
    "help",
    "version",
    "count-objects",
    "fsck",
    "whatchanged",
    "show-branch",
    "check-attr",
    "cherry",
    "range-diff",
    "annotate",
    "verify-commit",
}


def _git_sub(argv: list[str]) -> tuple[str, list[str]]:
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in _GIT_GLOBAL_WITH_VALUE else 1
    if i >= len(argv):
        return "", []
    return argv[i], [argv[i], *argv[i + 1 :]]


# `git -c KEY=VALUE` keys that cannot name a program to run; any other key (core.fsmonitor,
# core.pager, core.sshCommand, alias.*, filter.*, include.path, ...) can run arbitrary code
_GIT_SAFE_CONFIG = (
    "color.",
    "advice.",
    "log.",
    "format.pretty",
    "user.name",
    "user.email",
    "core.quotepath",
    "core.autocrlf",
    "init.defaultbranch",
    "commit.gpgsign",
    "tag.gpgsign",
    "merge.conflictstyle",
    "diff.renames",
    "pull.rebase",
    "column.",
)
# options that make git run a given program
_GIT_RUNS = ("--upload-pack", "--receive-pack", "--exec", "--template")


def _git_config_runs_code(argv: list[str]) -> bool:
    values = option_values(argv, "-c", "--config")
    if any(not v.lower().split("=", 1)[0].startswith(_GIT_SAFE_CONFIG) for v in values):
        return True
    return has(argv, "--config-env", "--exec-path")


def rule_git(argv: list[str]) -> Part:
    sub, rest = _git_sub(argv)
    a = rest  # rest[0] is the subcommand, so has()/positional() work on it
    pos = positional(a)
    global_opts = argv[: len(argv) - len(rest)] if rest else argv
    if _git_config_runs_code(global_opts) or (sub == "clone" and _git_config_runs_code(a)):
        return P(argv, A.UNKNOWN, "git config on the command line can run arbitrary programs")
    if not sub:
        return P(argv, A.SAFE_READ, "git without a subcommand")
    upload_pack_u = sub in {"clone", "ls-remote"} and has(a, "-u")  # push -u is --set-upstream
    if upload_pack_u or (
        sub in {"fetch", "clone", "pull", "push", "ls-remote", "archive"} and has(a, *_GIT_RUNS)
    ):
        return P(argv, A.UNKNOWN, f"git {sub} with a program to run")
    if (sub == "grep" and has(a, "-O", "--open-files-in-pager")) or (
        sub in {"submodule", "bisect"} and pos[:1] in (["foreach"], ["run"])
    ):
        return P(argv, A.UNKNOWN, f"git {sub} runs a command")
    if sub in _GIT_READ:
        if sub == "diff" and has(a, "--output"):
            return P(argv, A.WORKTREE_WRITE, "git diff --output writes a file")
        return P(argv, A.SAFE_READ, f"git {sub} only reads")
    if sub == "branch":
        if has(a, "-d", "-D", "--delete"):
            return P(argv, A.DESTRUCTIVE, "deletes a branch")
        if has(a, "-m", "-M", "-c", "-C", "--move", "--copy", "-f", "--force"):
            return P(argv, A.LOCAL_STATE_CHANGE, "renames/copies/forces a branch")
        if pos and not has(a, "--list", "-l", "-a", "-r", "--contains", "--merged"):
            return P(argv, A.LOCAL_STATE_CHANGE, "creates a branch")
        return P(argv, A.SAFE_READ, "lists branches")
    if sub == "tag":
        if has(a, "-d", "--delete"):
            return P(argv, A.DESTRUCTIVE, "deletes a tag")
        if pos and not has(a, "-l", "--list"):
            return P(argv, A.LOCAL_STATE_CHANGE, "creates a tag")
        return P(argv, A.SAFE_READ, "lists tags")
    if sub == "remote":
        if not pos or pos[0] in {"-v", "get-url"} or (pos and pos[0] == "get-url"):
            return P(argv, A.SAFE_READ, "lists remotes")
        if pos[0] in {"show", "update", "prune"}:
            cls = A.REMOTE_READ if pos[0] != "prune" else A.LOCAL_STATE_CHANGE
            return P(argv, cls, f"git remote {pos[0]} contacts the remote", N.GIT_REMOTE)
        if pos[0] in {"remove", "rm"}:
            return P(argv, A.LOCAL_DESTRUCTIVE, "removes a remote")
        return P(argv, A.LOCAL_STATE_CHANGE, f"git remote {pos[0]} changes config")
    if sub == "config":
        if (
            has(a, "--get", "--get-all", "--list", "-l", "--get-regexp", "--show-origin")
            or len(pos) <= 1
        ):
            return P(argv, A.SAFE_READ, "reads git config")
        if has(a, "--global", "--system"):
            return P(argv, A.PRIVILEGED, "changes git config outside the repository")
        return P(argv, A.LOCAL_STATE_CHANGE, "changes repository git config")
    if sub == "stash":
        action = pos[0] if pos else "push"
        if action in {"list", "show"}:
            return P(argv, A.SAFE_READ, "reads stashes")
        if action in {"drop", "clear"}:
            return P(argv, A.DESTRUCTIVE, f"git stash {action} discards stashed work")
        return P(argv, A.LOCAL_STATE_CHANGE, f"git stash {action}")
    if sub == "reflog":
        if pos and pos[0] in {"expire", "delete"}:
            return P(argv, A.DESTRUCTIVE, "rewrites the reflog")
        return P(argv, A.SAFE_READ, "reads the reflog")
    if sub == "worktree":
        action = pos[0] if pos else "list"
        if action == "list":
            return P(argv, A.SAFE_READ, "lists worktrees")
        if action == "remove" and has(a, "-f", "--force"):
            return P(argv, A.DESTRUCTIVE, "force-removes a worktree")
        if action in {"remove", "prune"}:
            return P(argv, A.LOCAL_DESTRUCTIVE, f"git worktree {action}")
        return P(argv, A.LOCAL_STATE_CHANGE, f"git worktree {action}")
    if sub == "reset":
        if has(a, "--hard"):
            return P(argv, A.DESTRUCTIVE, "git reset --hard discards work")
        if has(a, "--keep", "--merge"):
            return P(argv, A.LOCAL_DESTRUCTIVE, "git reset may discard work")
        # soft/mixed resets keep the working tree and the reflog: a state change (spec table)
        return P(argv, A.LOCAL_STATE_CHANGE, "moves HEAD / unstages")
    if sub == "clean":
        if has(a, "-n", "--dry-run"):
            return P(argv, A.SAFE_READ, "git clean dry run")
        return P(argv, A.DESTRUCTIVE, "git clean deletes untracked files")
    if sub in {"checkout", "switch", "restore"}:
        if has(a, "-f", "--force", "--discard-changes"):
            return P(argv, A.DESTRUCTIVE, f"git {sub} --force discards work")
        creates = has(a, "-b", "-B", "--orphan", "-t", "--track")
        ref_and_paths = sub == "checkout" and len(pos) >= 2 and not creates
        if sub == "restore" or "--" in a or (sub == "checkout" and "." in pos) or ref_and_paths:
            if sub == "restore" and has(a, "--staged", "-S") and not has(a, "--worktree", "-W"):
                return P(argv, A.LOCAL_STATE_CHANGE, "unstages paths")
            return P(argv, A.LOCAL_DESTRUCTIVE, f"git {sub} overwrites working-tree files")
        return P(argv, A.LOCAL_STATE_CHANGE, f"git {sub} changes the checked-out ref")
    if sub == "commit":
        if has(a, "--amend"):
            return P(argv, A.DESTRUCTIVE, "git commit --amend rewrites history")
        return P(argv, A.LOCAL_STATE_CHANGE, "creates a commit")
    if sub in {"rebase", "filter-branch", "filter-repo", "replace"}:
        return P(argv, A.DESTRUCTIVE, f"git {sub} rewrites history")
    if sub == "update-ref":
        return P(argv, A.DESTRUCTIVE, "git update-ref rewrites refs")
    if sub == "gc":
        if any(x.startswith("--prune") for x in a):
            return P(argv, A.DESTRUCTIVE, "git gc --prune drops unreachable objects")
        return P(argv, A.LOCAL_STATE_CHANGE, "git gc")
    if sub in {"add", "mv", "rm", "apply"}:
        if sub == "rm" and has(a, "-r") and has(a, "-f"):
            return P(argv, A.LOCAL_DESTRUCTIVE, "git rm -rf")
        return P(argv, A.WORKTREE_WRITE, f"git {sub} changes tracked files")
    if sub in {"merge", "cherry-pick", "revert", "am", "init", "notes"}:
        if sub == "merge" and has(a, "--abort"):
            return P(argv, A.LOCAL_DESTRUCTIVE, "aborts a merge")
        return P(argv, A.LOCAL_STATE_CHANGE, f"git {sub}")
    if sub in {"fetch", "ls-remote"}:
        return P(argv, A.REMOTE_READ, f"git {sub} contacts the remote", N.GIT_REMOTE)
    if sub == "pull":
        return P(argv, A.LOCAL_STATE_CHANGE, "git pull fetches and merges", N.GIT_REMOTE)
    if sub == "clone":
        url = pos[0] if pos else ""
        return P(argv, A.REMOTE_READ, "git clone", N.GIT_REMOTE, host_of(url) or None)
    if sub == "submodule":
        if pos and pos[0] in {"status", "summary"}:
            return P(argv, A.SAFE_READ, "reads submodules")
        if pos and pos[0] in {"update", "sync", "add"}:
            return P(argv, A.REMOTE_READ, f"git submodule {pos[0]}", N.GIT_REMOTE)
        return P(argv, A.LOCAL_STATE_CHANGE, "git submodule")
    if sub == "push":
        if has(
            a,
            "-f",
            "--force",
            "--force-with-lease",
            "--force-if-includes",
            "--mirror",
            "-d",
            "--delete",
            "--prune",
        ) or any(p.startswith("+") or p.startswith(":") for p in pos[1:]):
            return P(
                argv,
                A.DESTRUCTIVE_REMOTE_WRITE,
                "force/delete push rewrites the remote",
                N.GIT_REMOTE,
            )
        return P(argv, A.REMOTE_WRITE, "git push", N.GIT_REMOTE)
    if sub == "lfs":
        if pos and pos[0] in {"ls-files", "status", "env", "version"}:
            return P(argv, A.SAFE_READ, "git lfs read")
        return P(argv, A.REMOTE_READ, "git lfs transfers objects", N.GIT_REMOTE)
    if sub == "bisect":
        return P(argv, A.LOCAL_STATE_CHANGE, "git bisect moves HEAD")
    return P(argv, A.UNKNOWN, f"no rule for git {sub}")


# ---------------------------------------------------------------- GitHub CLI


def _gh_fields(argv: list[str]) -> dict[str, str]:
    """`gh api` request fields (-f/-F/--field/--raw-field key=value)."""
    fields: dict[str, str] = {}
    for i, arg in enumerate(argv):
        value = None
        if arg in {"-f", "-F", "--field", "--raw-field"} and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith(("--field=", "--raw-field=")):
            value = arg.split("=", 1)[1]
        if value is not None and "=" in value:
            key, val = value.split("=", 1)
            fields[key] = val
    return fields


def rule_gh(argv: list[str]) -> Part:
    pos = positional(
        argv,
        frozenset(
            {
                "-R",
                "--repo",
                "-X",
                "--method",
                "-H",
                "--header",
                "-q",
                "--jq",
                "-f",
                "-F",
                "--field",
                "--raw-field",
                "-t",
                "--template",
                "-L",
                "--limit",
                "-s",
                "--state",
                "--json",
            }
        ),
    )
    group = pos[0] if pos else ""
    action = pos[1] if len(pos) > 1 else ""
    if group == "auth":
        if action == "token" or has(argv, "--show-token", "-t"):
            return P(argv, A.PRIVILEGED, "prints GitHub credentials")
        if action == "status":
            return P(argv, A.REMOTE_READ, "gh auth status", N.GITHUB)
        return P(argv, A.PRIVILEGED, "changes GitHub credentials")
    if group == "api":
        # `-XDELETE` is the combined short form of `-X DELETE`
        combined = [a[2:] for a in argv[1:] if a.startswith("-X") and len(a) > 2]
        explicit = option_value(argv, "-X", "--method") or (combined[0] if combined else None)
        fields = _gh_fields(argv)
        if action == "graphql" and explicit is None and not has(argv, "--input"):
            query = fields.get("query", "")
            if (
                query
                and not query.startswith("@")  # -F query=@file: the operation is not visible
                and not re.search(
                    r"\b(mutation|subscription)\b", query
                )  # anywhere: comments can hide it
                and set(fields) <= {"query", "owner", "repo", "number", "cursor", "endCursor"}
            ):
                return P(argv, A.REMOTE_READ, "gh api graphql query", N.GITHUB)
            return P(argv, A.REMOTE_WRITE, "gh api graphql mutation or unknown operation", N.GITHUB)
        method = (explicit or ("POST" if fields or has(argv, "--input") else "GET")).upper()
        if method == "DELETE":
            return P(argv, A.DESTRUCTIVE_REMOTE_WRITE, "gh api DELETE", N.GITHUB)
        if method != "GET":
            return P(argv, A.REMOTE_WRITE, f"gh api {method}", N.GITHUB)
        return P(argv, A.REMOTE_READ, "gh api GET", N.GITHUB)
    reads = {"view", "list", "status", "diff", "checks", "watch"}
    if (
        action in reads
        or group in {"search"}
        or (group == "run" and action in {"view", "list", "watch"})
    ):
        if group in {"release", "run"} and action == "download":
            return P(argv, A.REMOTE_READ, f"gh {group} download", N.GITHUB)
        return P(argv, A.REMOTE_READ, f"gh {group} {action}", N.GITHUB)
    if action == "download":
        return P(argv, A.REMOTE_READ, f"gh {group} download", N.GITHUB)
    if action in {"delete", "archive"} or (
        group == "repo" and action in {"delete", "rename", "archive"}
    ):
        return P(argv, A.DESTRUCTIVE_REMOTE_WRITE, f"gh {group} {action}", N.GITHUB)
    if group == "pr" and action == "checkout":
        return P(argv, A.LOCAL_STATE_CHANGE, "gh pr checkout switches branches", N.GITHUB)
    if group == "repo" and action == "clone":
        return P(argv, A.REMOTE_READ, "gh repo clone", N.GITHUB)
    if group:
        return P(argv, A.REMOTE_WRITE, f"gh {group} {action} changes GitHub state", N.GITHUB)
    return P(argv, A.SAFE_READ, "gh help")


# ---------------------------------------------------------------- Rust


def rule_cargo(argv: list[str]) -> Part:
    pos = positional(
        argv,
        frozenset(
            {
                "-p",
                "--package",
                "--manifest-path",
                "-j",
                "--jobs",
                "--target",
                "--features",
                "-F",
                "--bin",
                "--example",
                "--test",
                "--bench",
                "--profile",
                "-Z",
            }
        ),
    )
    sub = pos[0] if pos else ""
    if sub.startswith("+") and len(pos) > 1:
        sub = pos[1]
    if sub in {
        "check",
        "build",
        "b",
        "c",
        "test",
        "t",
        "bench",
        "doc",
        "run",
        "r",
        "nextest",
        "miri",
        "llvm-cov",
        "expand",
    }:
        return P(argv, A.SAFE_BUILD, f"cargo {sub} builds/runs repository code")
    if sub in {
        "metadata",
        "tree",
        "version",
        "pkgid",
        "locate-project",
        "verify-project",
        "read-manifest",
        "help",
        "--version",
        "-V",
    }:
        return P(argv, A.SAFE_READ, f"cargo {sub}")
    if sub == "clippy":
        if has(argv, "--fix"):
            return P(argv, A.WORKTREE_WRITE, "cargo clippy --fix edits sources")
        return P(argv, A.SAFE_BUILD, "cargo clippy")
    if sub == "fmt":
        if has(argv, "--check"):
            return P(argv, A.SAFE_BUILD, "cargo fmt --check")
        return P(argv, A.WORKTREE_WRITE, "cargo fmt rewrites sources")
    if sub in {"fix", "new", "init"}:
        return P(argv, A.WORKTREE_WRITE, f"cargo {sub} writes files")
    if sub in {
        "fetch",
        "update",
        "add",
        "remove",
        "rm",
        "install",
        "search",
        "vendor",
        "generate-lockfile",
        "uninstall",
    }:
        return P(
            argv, A.PACKAGE_NETWORK, f"cargo {sub} uses the package registry", N.PACKAGE_REGISTRIES
        )
    if sub in {"publish", "yank", "owner", "login", "logout"}:
        return P(argv, A.REMOTE_WRITE, f"cargo {sub} changes crates.io", N.PACKAGE_REGISTRIES)
    if sub == "clean":
        return P(argv, A.LOCAL_STATE_CHANGE, "cargo clean removes build artifacts")
    return P(argv, A.UNKNOWN, f"no rule for cargo {sub}")


def rule_rustfmt(argv: list[str]) -> Part:
    if has(argv, "--check"):
        return P(argv, A.SAFE_BUILD, "rustfmt --check")
    return P(argv, A.WORKTREE_WRITE, "rustfmt rewrites sources")


def rule_rustup(argv: list[str]) -> Part:
    pos = positional(argv)
    if (
        not pos
        or pos[0] in {"show", "which", "--version", "doc", "help"}
        or pos[:2] in (["toolchain", "list"], ["target", "list"], ["component", "list"])
    ):
        return P(argv, A.SAFE_READ, "rustup read")
    return P(argv, A.PACKAGE_NETWORK, "rustup downloads toolchains", N.PACKAGE_REGISTRIES)


# ---------------------------------------------------------------- Python


def rule_python(argv: list[str]) -> Part:
    if len(argv) == 1:
        return P(argv, A.UNKNOWN, "interactive interpreter")
    if has(argv, "-c"):
        return P(argv, A.UNKNOWN, "python -c runs inline code")
    if argv[1] in {"--version", "-V"}:
        return P(argv, A.SAFE_READ, "python --version")
    if "-m" in argv:
        module = argv[argv.index("-m") + 1] if argv.index("-m") + 1 < len(argv) else ""
        rest = argv[argv.index("-m") + 1 :]
        if module in RULES and module not in {"python", "python3"}:
            return RULES[module](rest)
        if module in {"http.server", "SimpleHTTPServer"}:
            return P(argv, A.LOCAL_STATE_CHANGE, "starts an HTTP server")
        if module in {"venv", "virtualenv"}:
            return P(argv, A.LOCAL_STATE_CHANGE, "creates a virtualenv")
        if module in {
            "json.tool",
            "pydoc",
            "tokenize",
            "ast",
            "dis",
            "site",
            "sysconfig",
            "platform",
        }:
            return P(argv, A.SAFE_READ, f"python -m {module}")
        return P(argv, A.SAFE_BUILD, f"python -m {module} runs repository code")
    return P(argv, A.SAFE_BUILD, "runs a Python script (sandboxed)")


def rule_pip(argv: list[str]) -> Part:
    pos = positional(argv, frozenset({"-r", "-c", "-i", "--index-url", "-e", "--target", "-t"}))
    sub = pos[0] if pos else ""
    if sub in {
        "list",
        "show",
        "freeze",
        "check",
        "--version",
        "help",
        "inspect",
        "config",
        "debug",
        "cache",
    }:
        return P(argv, A.SAFE_READ, f"pip {sub}")
    if sub in {"install", "download", "wheel", "index"}:
        return P(
            argv, A.PACKAGE_NETWORK, f"pip {sub} uses the package registry", N.PACKAGE_REGISTRIES
        )
    if sub == "uninstall":
        return P(argv, A.LOCAL_STATE_CHANGE, "pip uninstall")
    return P(argv, A.UNKNOWN, f"no rule for pip {sub}")


def rule_uv(argv: list[str]) -> Part:
    pos = positional(
        argv,
        frozenset(
            {
                "--python",
                "-p",
                "--directory",
                "--project",
                "--with",
                "--index-url",
                "--extra",
                "--group",
                "--package",
            }
        ),
    )
    sub = pos[0] if pos else ""
    sub2 = pos[1] if len(pos) > 1 else ""
    if (
        sub in {"--version", "version", "help", "tree"}
        or (sub == "pip" and sub2 in {"list", "show", "freeze", "check", "tree"})
        or (sub == "python" and sub2 in {"list", "find", "dir"})
        or (sub == "tool" and sub2 in {"list", "dir"})
        or (sub == "cache" and sub2 in {"dir"})
    ):
        return P(argv, A.SAFE_READ, f"uv {sub} {sub2}".strip())
    if (
        sub in {"sync", "lock", "add", "remove"}
        or (sub == "pip" and sub2 in {"install", "sync", "compile"})
        or (sub in {"python", "tool"} and sub2 in {"install", "upgrade"})
    ):
        return P(
            argv,
            A.PACKAGE_NETWORK,
            f"uv {sub} {sub2} resolves packages".replace("  ", " "),
            N.PACKAGE_REGISTRIES,
        )
    if (sub == "tool" and sub2 == "run") or sub == "x" or argv[0] == "uvx":
        return P(argv, A.PACKAGE_NETWORK, "uvx downloads and runs a tool", N.PACKAGE_REGISTRIES)
    if sub == "build":
        return P(argv, A.SAFE_BUILD, "uv build")
    if sub == "publish":
        return P(argv, A.REMOTE_WRITE, "uv publish uploads a package", N.PACKAGE_REGISTRIES)
    if (
        sub == "venv"
        or (sub == "cache" and sub2 in {"clean", "prune"})
        or (sub == "pip" and sub2 == "uninstall")
        or (sub in {"python", "tool"} and sub2 == "uninstall")
    ):
        return P(argv, A.LOCAL_STATE_CHANGE, f"uv {sub} {sub2}".strip())
    if sub == "init":
        return P(argv, A.WORKTREE_WRITE, "uv init writes files")
    if sub == "export":
        return P(argv, A.SAFE_READ, "uv export")
    return P(argv, A.UNKNOWN, f"no rule for uv {sub}")


def rule_poetry(argv: list[str]) -> Part:
    sub = (positional(argv) or [""])[0]
    if sub in {"show", "check", "env", "version", "about", "config"}:
        return P(argv, A.SAFE_READ, f"poetry {sub}")
    if sub in {"install", "add", "remove", "update", "lock", "sync"}:
        return P(argv, A.PACKAGE_NETWORK, f"poetry {sub}", N.PACKAGE_REGISTRIES)
    if sub == "build":
        return P(argv, A.SAFE_BUILD, "poetry build")
    if sub == "publish":
        return P(argv, A.REMOTE_WRITE, "poetry publish", N.PACKAGE_REGISTRIES)
    return P(argv, A.UNKNOWN, f"no rule for poetry {sub}")


def rule_check_or_fix(write_flags: tuple[str, ...], check_flags: tuple[str, ...] = ()) -> Rule:
    def rule(argv: list[str]) -> Part:
        if check_flags and has(argv, *check_flags):
            return P(argv, A.SAFE_BUILD, f"{argv[0]} check mode")
        if has(argv, *write_flags):
            return P(argv, A.WORKTREE_WRITE, f"{argv[0]} rewrites files")
        return P(argv, A.SAFE_BUILD, f"{argv[0]} checks the code")

    return rule


def rule_ruff(argv: list[str]) -> Part:
    sub = (positional(argv) or [""])[0]
    if sub == "format" and not has(argv, "--check", "--diff"):
        return P(argv, A.WORKTREE_WRITE, "ruff format rewrites files")
    if has(argv, "--fix", "--unsafe-fixes") and not has(argv, "--no-fix"):
        return P(argv, A.WORKTREE_WRITE, "ruff --fix edits files")
    return P(argv, A.SAFE_BUILD, "ruff checks the code")


def rule_black(argv: list[str]) -> Part:
    if has(argv, "--check", "--diff"):
        return P(argv, A.SAFE_BUILD, "black --check")
    return P(argv, A.WORKTREE_WRITE, "black rewrites files")


def rule_safe_build(argv: list[str]) -> Part:
    return P(argv, A.SAFE_BUILD, f"{argv[0]} builds/tests repository code")


# ---------------------------------------------------------------- JavaScript / TypeScript

_JS_LOCAL_TOOLS = {
    "tsc",
    "eslint",
    "vitest",
    "jest",
    "prettier",
    "vite",
    "next",
    "tsx",
    "ts-node",
    "playwright",
    "mocha",
    "biome",
    "turbo",
    "nx",
    "webpack",
    "rollup",
    "esbuild",
}


def rule_node_pm(argv: list[str]) -> Part:
    pm = argv[0]
    pos = positional(
        argv, frozenset({"--filter", "-F", "-C", "--dir", "--prefix", "-w", "--workspace", "--cwd"})
    )
    sub = pos[0] if pos else ("install" if pm in {"yarn", "bun"} and len(argv) == 1 else "")
    if sub in {
        "install",
        "i",
        "ci",
        "add",
        "update",
        "up",
        "upgrade",
        "remove",
        "rm",
        "uninstall",
        "un",
        "dedupe",
        "link",
        "unlink",
        "prune",
        "import",
        "fetch",
        "rebuild",
    }:
        return P(
            argv,
            A.PACKAGE_NETWORK,
            f"{pm} {sub} resolves packages (lifecycle scripts run)",
            N.PACKAGE_REGISTRIES,
        )
    if sub in {"dlx", "create"} or (pm == "npm" and sub in {"init", "create"}):
        return P(
            argv,
            A.PACKAGE_NETWORK,
            f"{pm} {sub} downloads and runs a package",
            N.PACKAGE_REGISTRIES,
        )
    if sub in {"publish", "unpublish", "deprecate", "dist-tag", "owner", "access", "team"}:
        return P(argv, A.REMOTE_WRITE, f"{pm} {sub} changes the registry", N.PACKAGE_REGISTRIES)
    if sub in {"login", "adduser", "logout", "token"}:
        return P(argv, A.PRIVILEGED, f"{pm} {sub} handles registry credentials")
    if sub in {
        "ls",
        "list",
        "why",
        "explain",
        "config",
        "root",
        "bin",
        "prefix",
        "help",
        "--version",
        "-v",
        "licenses",
        "query",
    }:
        return P(argv, A.SAFE_READ, f"{pm} {sub}")
    if sub in {"view", "info", "show", "outdated", "audit", "search", "doctor"}:
        return P(argv, A.REMOTE_READ, f"{pm} {sub} queries the registry", N.PACKAGE_REGISTRIES)
    script = pos[1] if sub in {"run", "run-script"} and len(pos) > 1 else sub
    if any(r in script for r in ("deploy", "publish", "release")):
        return P(argv, A.REMOTE_WRITE, f"script '{script}' looks like a deploy/publish step")
    if (
        sub
        in {
            "test",
            "t",
            "run",
            "run-script",
            "start",
            "build",
            "lint",
            "typecheck",
            "dev",
            "check",
            "format",
            "fmt",
            "exec",
            "x",
        }
        or script
    ):
        return P(argv, A.SAFE_BUILD, f"{pm} runs repository script '{script}' (sandboxed)")
    return P(argv, A.UNKNOWN, f"no rule for {pm} {sub}")


def rule_npx(argv: list[str]) -> Part:
    pos = positional(argv, frozenset({"-p", "--package"}))
    tool = pos[0] if pos else ""
    if tool in _JS_LOCAL_TOOLS and not has(argv, "-y", "--yes", "-p", "--package"):
        inner = [tool, *argv[argv.index(tool) + 1 :]]
        return (
            RULES[tool](inner)
            if tool in RULES
            else P(inner, A.SAFE_BUILD, f"{tool} from node_modules")
        )
    return P(argv, A.PACKAGE_NETWORK, f"{argv[0]} may download '{tool}'", N.PACKAGE_REGISTRIES)


def rule_node(argv: list[str]) -> Part:
    if len(argv) == 1:
        return P(argv, A.UNKNOWN, "interactive node")
    if has(argv, "-e", "--eval", "-p", "--print"):
        return P(argv, A.UNKNOWN, "node -e runs inline code")
    if argv[1] in {"--version", "-v"}:
        return P(argv, A.SAFE_READ, "node --version")
    return P(argv, A.SAFE_BUILD, "runs a Node script (sandboxed)")


def rule_eslint(argv: list[str]) -> Part:
    return rule_check_or_fix(("--fix",))(argv)


def rule_prettier(argv: list[str]) -> Part:
    return rule_check_or_fix(("--write", "-w"), ("--check", "-c", "--list-different", "-l"))(argv)


# ---------------------------------------------------------------- Go


def rule_go(argv: list[str]) -> Part:
    pos = positional(argv, frozenset({"-C", "-o", "-run", "-tags", "-p", "-timeout", "-count"}))
    sub = pos[0] if pos else ""
    sub2 = pos[1] if len(pos) > 1 else ""
    if sub in {"build", "test", "vet", "run", "tool"}:
        return P(argv, A.SAFE_BUILD, f"go {sub}")
    if sub in {"list", "env", "version", "doc", "help"} and not has(argv, "-w"):
        return P(argv, A.SAFE_READ, f"go {sub}")
    if sub == "env" and has(argv, "-w", "-u"):
        return P(argv, A.LOCAL_STATE_CHANGE, "go env -w changes Go settings")
    if sub == "fmt" or sub == "fix":
        return P(argv, A.WORKTREE_WRITE, f"go {sub} rewrites sources")
    if sub == "generate":
        return P(argv, A.UNKNOWN, "go generate runs arbitrary commands from source comments")
    if (
        sub in {"get", "install"}
        or (sub == "mod" and sub2 in {"download", "tidy", "vendor"})
        or (sub == "work" and sub2 == "sync")
    ):
        return P(
            argv,
            A.PACKAGE_NETWORK,
            f"go {sub} {sub2} fetches modules".replace("  ", " "),
            N.PACKAGE_REGISTRIES,
        )
    if sub == "mod" and sub2 in {"graph", "why", "verify"}:
        return P(argv, A.SAFE_READ, f"go mod {sub2}")
    if sub in {"mod", "work"} and sub2 in {"init", "edit", "use"}:
        return P(argv, A.WORKTREE_WRITE, f"go {sub} {sub2} edits module files")
    if sub == "clean":
        if has(argv, "-modcache", "-cache"):
            return P(argv, A.LOCAL_DESTRUCTIVE, "go clean deletes caches")
        return P(argv, A.LOCAL_STATE_CHANGE, "go clean")
    return P(argv, A.UNKNOWN, f"no rule for go {sub}")


def rule_gofmt(argv: list[str]) -> Part:
    if has(argv, "-w"):
        return P(argv, A.WORKTREE_WRITE, "gofmt -w rewrites sources")
    return P(argv, A.SAFE_READ, "gofmt prints")


def rule_golangci(argv: list[str]) -> Part:
    return rule_check_or_fix(("--fix",))(argv)


# ---------------------------------------------------------------- Swift


def rule_swift(argv: list[str]) -> Part:
    pos = positional(
        argv,
        frozenset({"--package-path", "-c", "--configuration", "--filter", "--product", "--target"}),
    )
    sub = pos[0] if pos else ""
    sub2 = pos[1] if len(pos) > 1 else ""
    if sub in {"build", "test", "run"}:
        return P(argv, A.SAFE_BUILD, f"swift {sub}")
    if sub == "package":
        if sub2 in {"resolve", "update"}:
            return P(argv, A.PACKAGE_NETWORK, f"swift package {sub2}", N.PACKAGE_REGISTRIES)
        if sub2 in {"describe", "dump-package", "show-dependencies", "compute-checksum"}:
            return P(argv, A.SAFE_READ, f"swift package {sub2}")
        if sub2 in {"clean", "reset", "purge-cache"}:
            return P(argv, A.LOCAL_STATE_CHANGE, f"swift package {sub2}")
        if sub2 in {"init", "edit", "unedit", "add-dependency", "add-target"}:
            return P(argv, A.WORKTREE_WRITE, f"swift package {sub2}")
        return P(argv, A.UNKNOWN, f"swift package {sub2} (plugins run code)")
    if sub == "format":
        if sub2 == "lint" or not ({"-i", "--in-place"} & set(argv)):
            return P(argv, A.SAFE_BUILD, "swift format lint")
        return P(argv, A.WORKTREE_WRITE, "swift format --in-place rewrites sources")
    if sub in {"--version", "-version"}:
        return P(argv, A.SAFE_READ, "swift --version")
    return P(argv, A.UNKNOWN, f"no rule for swift {sub}")


def rule_xcodebuild(argv: list[str]) -> Part:
    if has(argv, "-list", "-showBuildSettings", "-version", "-showsdks", "-showdestinations"):
        return P(argv, A.SAFE_READ, "xcodebuild query")
    if any(a in {"archive", "-exportArchive"} for a in argv):
        return P(argv, A.LOCAL_STATE_CHANGE, "xcodebuild archive/export")
    return P(argv, A.SAFE_BUILD, "xcodebuild build/test")


# ---------------------------------------------------------------- Solidity / Cairo / chains


def _rpc(argv: list[str], *names: str) -> tuple[bool, str | None]:
    """(is local, url) for an --rpc-url style option; missing means not known to be local."""
    url = option_value(argv, *names)
    if url is None:
        return False, None
    return is_loopback(host_of(url)), url


def rule_forge(argv: list[str]) -> Part:
    pos = positional(
        argv,
        frozenset(
            {
                "--rpc-url",
                "-r",
                "--fork-url",
                "-f",
                "--match-test",
                "--mt",
                "--match-contract",
                "--mc",
                "--match-path",
                "--mp",
                "--private-key",
                "--sender",
                "-C",
                "--root",
            }
        ),
    )
    sub = pos[0] if pos else ""
    if has(argv, "--private-key", "--mnemonic", "--mnemonic-path"):
        return P(argv, A.PRIVILEGED, "a private key or mnemonic on the command line")
    if sub in {"build", "b", "test", "t", "snapshot", "coverage", "compile", "doc", "geiger"}:
        fork_local, fork = _rpc(argv, "--fork-url", "-f", "--rpc-url")
        if fork is not None and not fork_local:
            return P(
                argv,
                A.REMOTE_READ,
                f"forge {sub} forks a remote chain",
                N.BLOCKCHAIN_RPC_READ,
                host_of(fork),
            )
        return P(argv, A.SAFE_BUILD, f"forge {sub}")
    if sub in {"inspect", "config", "tree", "remappings", "selectors", "--version"}:
        return P(argv, A.SAFE_READ, f"forge {sub}")
    if sub == "fmt":
        return (
            rule_check_or_fix((), ("--check",))(argv)
            if has(argv, "--check")
            else P(argv, A.WORKTREE_WRITE, "forge fmt rewrites sources")
        )
    if sub in {"install", "update", "i"}:
        return P(argv, A.PACKAGE_NETWORK, f"forge {sub} fetches dependencies from git", N.GITHUB)
    if sub in {"init", "remove", "bind"}:
        return P(argv, A.WORKTREE_WRITE, f"forge {sub} writes files")
    if sub == "clean":
        return P(argv, A.LOCAL_STATE_CHANGE, "forge clean")
    local, url = _rpc(argv, "--rpc-url", "-r", "--fork-url", "-f")
    if sub == "script":
        if has(argv, "--broadcast", "--resume"):
            if local:
                return P(argv, A.SAFE_LOCAL, "broadcasts to a local chain")
            return P(
                argv,
                A.REMOTE_WRITE,
                "forge script --broadcast sends transactions",
                N.BLOCKCHAIN_RPC_WRITE,
                host_of(url) if url else None,
            )
        if url and not local:
            return P(
                argv,
                A.REMOTE_READ,
                "forge script simulates against a remote chain",
                N.BLOCKCHAIN_RPC_READ,
                host_of(url),
            )
        return P(argv, A.SAFE_BUILD, "forge script simulation")
    if sub in {"create", "verify-contract", "verify-check"}:
        if sub == "create" and local:
            return P(argv, A.SAFE_LOCAL, "deploys to a local chain")
        net = N.BLOCKCHAIN_RPC_WRITE if sub == "create" else N.OTHER
        return P(
            argv,
            A.REMOTE_WRITE,
            f"forge {sub} publishes on-chain/explorer",
            net,
            host_of(url) if url else None,
        )
    return P(argv, A.UNKNOWN, f"no rule for forge {sub}")


_CAST_PURE = {
    "abi-encode",
    "abi-decode",
    "calldata",
    "calldata-decode",
    "decode-calldata",
    "keccak",
    "keccak256",
    "sig",
    "selectors",
    "to-wei",
    "from-wei",
    "to-hex",
    "to-dec",
    "to-uint256",
    "to-int256",
    "to-bytes32",
    "to-ascii",
    "to-utf8",
    "from-utf8",
    "concat-hex",
    "max-uint",
    "min-int",
    "max-int",
    "address-zero",
    "hash-zero",
    "pretty-calldata",
    "compute-address",
    "create2",
    "format-bytes32-string",
    "parse-bytes32-string",
    "shl",
    "shr",
    "--version",
    "index",
    "decode-abi",
}
_CAST_WRITE = {"send", "s", "publish", "mktx"}


COSMOS_DAEMONS = (
    "gaiad",
    "osmosisd",
    "junod",
    "wasmd",
    "simd",
    "neutrond",
    "injectived",
    "evmosd",
    "celestia-appd",
    "stargazed",
    "akash",
    "secretd",
    "terrad",
    "kujirad",
    "dydxprotocold",
)


def rule_cosmos(argv: list[str]) -> Part:
    """Cosmos SDK chain daemons: `tx` signs and broadcasts, `query`/`q`/`status` read."""
    pos = positional(
        argv,
        frozenset(
            {
                "--node",
                "--chain-id",
                "--from",
                "--home",
                "--keyring-backend",
                "--fees",
                "--gas",
                "-o",
                "--output",
            }
        ),
    )
    sub = pos[0] if pos else ""
    if sub == "keys" or has(argv, "--mnemonic", "--recover"):
        if (
            len(pos) > 1
            and pos[1] in {"list", "show"}
            and not has(argv, "--unsafe", "--unarmored-hex")
        ):
            return P(argv, A.SAFE_READ, f"{argv[0]} keys {pos[1]}")
        return P(argv, A.PRIVILEGED, KEY_MATERIAL)
    node = option_value(argv, "--node")
    local = node is not None and is_loopback(host_of(node))
    if sub == "tx":
        if local:
            return P(argv, A.SAFE_LOCAL, f"{argv[0]} tx on a local chain")
        return P(
            argv,
            A.REMOTE_WRITE,
            f"{argv[0]} tx signs and broadcasts a transaction",
            N.BLOCKCHAIN_RPC_WRITE,
        )
    if sub in {"query", "q", "status"}:
        if local:
            return P(argv, A.SAFE_LOCAL, f"{argv[0]} {sub} against a local node")
        return P(argv, A.REMOTE_READ, f"{argv[0]} {sub} reads chain state", N.BLOCKCHAIN_RPC_READ)
    if sub in {"version", "config", "help", "debug"}:
        return P(argv, A.SAFE_READ, f"{argv[0]} {sub}")
    if sub in {
        "init",
        "start",
        "testnet",
        "collect-gentxs",
        "genesis",
        "add-genesis-account",
        "gentx",
    }:
        return P(argv, A.LOCAL_STATE_CHANGE, f"{argv[0]} {sub} (local chain setup)")
    return P(argv, A.UNKNOWN, f"no rule for {argv[0]} {sub}")


def rule_protoc(argv: list[str]) -> Part:
    if any(a.startswith("--plugin") for a in argv):
        return P(argv, A.UNKNOWN, "protoc with a custom plugin binary")
    if any(a.endswith("_out") or "_out=" in a for a in argv):
        return P(argv, A.WORKTREE_WRITE, "protoc generates code")
    return P(argv, A.SAFE_BUILD, "protoc")


def rule_buf(argv: list[str]) -> Part:
    sub = (positional(argv, frozenset({"--template", "-o", "--output", "--path"})) or [""])[0]
    if sub in {"lint", "build", "breaking", "ls-files", "--version"} or (
        sub == "format" and not has(argv, "-w", "--write")
    ):
        return P(argv, A.SAFE_BUILD, f"buf {sub}")
    if sub in {"generate", "format"}:
        return P(
            argv,
            A.WORKTREE_WRITE,
            f"buf {sub} writes generated files (remote plugins need network)",
        )
    if sub in {"dep", "mod"}:
        return P(argv, A.PACKAGE_NETWORK, f"buf {sub} fetches modules", N.PACKAGE_REGISTRIES)
    if sub in {"push", "registry"}:
        return P(argv, A.REMOTE_WRITE, f"buf {sub} changes the registry", N.PACKAGE_REGISTRIES)
    return P(argv, A.UNKNOWN, f"no rule for buf {sub}")


def rule_sqlc(argv: list[str]) -> Part:
    sub = (positional(argv, frozenset({"-f", "--file"})) or [""])[0]
    if sub == "generate":
        return P(argv, A.WORKTREE_WRITE, "sqlc generates code")
    if sub in {"compile", "vet", "version", "diff"}:
        return P(argv, A.SAFE_BUILD, f"sqlc {sub}")
    return P(argv, A.UNKNOWN, f"no rule for sqlc {sub}")


def rule_cast(argv: list[str]) -> Part:
    pos = positional(
        argv,
        frozenset(
            {
                "--rpc-url",
                "-r",
                "--private-key",
                "--from",
                "-f",
                "--value",
                "--gas-limit",
                "--keystore",
                "--account",
                "--chain",
            }
        ),
    )
    sub = pos[0] if pos else ""
    if has(argv, "--private-key", "--mnemonic") or (
        sub == "wallet"
        and len(pos) > 1
        and pos[1] in {"new", "import", "private-key", "decrypt-keystore", "sign", "new-mnemonic"}
    ):
        return P(argv, A.PRIVILEGED, KEY_MATERIAL)
    if sub in _CAST_PURE:
        return P(argv, A.SAFE_LOCAL, f"cast {sub} is offline")
    local, url = _rpc(argv, "--rpc-url", "-r")
    target = host_of(url) if url else None
    if sub in _CAST_WRITE:
        if local:
            return P(argv, A.SAFE_LOCAL, f"cast {sub} to a local chain")
        return P(
            argv, A.REMOTE_WRITE, f"cast {sub} sends a transaction", N.BLOCKCHAIN_RPC_WRITE, target
        )
    if sub == "rpc":
        method = pos[1] if len(pos) > 1 else ""
        if method.startswith("eth_send") and not local:
            return P(argv, A.REMOTE_WRITE, f"cast rpc {method}", N.BLOCKCHAIN_RPC_WRITE, target)
    if local:
        return P(argv, A.SAFE_LOCAL, f"cast {sub} against a local node")
    return P(argv, A.REMOTE_READ, f"cast {sub} reads chain state", N.BLOCKCHAIN_RPC_READ, target)


def rule_anvil(argv: list[str]) -> Part:
    fork = option_value(argv, "--fork-url", "-f")
    if fork and not is_loopback(host_of(fork)):
        return P(
            argv, A.REMOTE_READ, "anvil forks a remote chain", N.BLOCKCHAIN_RPC_READ, host_of(fork)
        )
    return P(argv, A.LOCAL_STATE_CHANGE, "starts a local chain")


def rule_scarb(argv: list[str]) -> Part:
    sub = (positional(argv, frozenset({"-p", "--package", "--manifest-path"})) or [""])[0]
    if sub in {"build", "test", "check", "cairo-run", "execute"}:
        return P(argv, A.SAFE_BUILD, f"scarb {sub}")
    if sub in {"metadata", "--version", "manifest-path", "commands", "help"}:
        return P(argv, A.SAFE_READ, f"scarb {sub}")
    if sub == "fmt":
        return (
            P(argv, A.SAFE_BUILD, "scarb fmt --check")
            if has(argv, "--check")
            else P(argv, A.WORKTREE_WRITE, "scarb fmt rewrites sources")
        )
    if sub in {"fetch", "add", "rm", "remove", "update"}:
        return P(argv, A.PACKAGE_NETWORK, f"scarb {sub}", N.PACKAGE_REGISTRIES)
    if sub == "publish":
        return P(argv, A.REMOTE_WRITE, "scarb publish", N.PACKAGE_REGISTRIES)
    if sub in {"new", "init"}:
        return P(argv, A.WORKTREE_WRITE, f"scarb {sub}")
    if sub == "clean":
        return P(argv, A.LOCAL_STATE_CHANGE, "scarb clean")
    return P(argv, A.UNKNOWN, f"no rule for scarb {sub}")


def rule_starknet_cli(argv: list[str]) -> Part:
    pos = positional(
        argv, frozenset({"--url", "--rpc", "--account", "--keystore", "--network", "--private-key"})
    )
    sub = pos[0] if pos else ""
    if has(argv, "--private-key") or (
        sub in {"signer", "keystore"}
        and len(pos) > 1
        and pos[1] in {"new", "from-key", "inspect-private", "export"}
    ):
        return P(argv, A.PRIVILEGED, KEY_MATERIAL)
    local, url = _rpc(argv, "--url", "--rpc")
    target = host_of(url) if url else None
    if sub in {"invoke", "deploy", "declare", "multicall", "transfer", "account"} or (
        sub == "account" and len(pos) > 1 and pos[1] in {"deploy", "create"}
    ):
        if local:
            return P(argv, A.SAFE_LOCAL, f"{argv[0]} {sub} on a local devnet")
        return P(
            argv,
            A.REMOTE_WRITE,
            f"{argv[0]} {sub} sends a transaction",
            N.BLOCKCHAIN_RPC_WRITE,
            target,
        )
    if local:
        return P(argv, A.SAFE_LOCAL, f"{argv[0]} {sub} against a local devnet")
    return P(
        argv, A.REMOTE_READ, f"{argv[0]} {sub} reads chain state", N.BLOCKCHAIN_RPC_READ, target
    )


# ---------------------------------------------------------------- containers / clusters / infra


def rule_docker(argv: list[str]) -> Part:
    pos = positional(
        argv,
        frozenset(
            {
                "-f",
                "--file",
                "-p",
                "--project-name",
                "--context",
                "-H",
                "--host",
                "-t",
                "--tag",
                "--name",
                "-e",
                "--env",
                "-v",
                "--volume",
                "--network",
                "-w",
                "--workdir",
                "--platform",
                "--format",
                "--filter",
                "-u",
                "--user",
                "--entrypoint",
                "--env-file",
                "--profile",
                "--mount",
                "-l",
                "--label",
                "--pid",
                "--cap-add",
            }
        ),
    )
    sub = pos[0] if pos else ""
    if (
        sub in {"image", "container"}
        and len(pos) > 1
        and pos[1] in {"pull", "push", "run", "build"}
    ):
        sub, pos = pos[1], pos[1:]  # `docker image pull` == `docker pull`
    if sub == "compose" or argv[0] == "docker-compose":
        csub = (
            pos[1]
            if sub == "compose" and len(pos) > 1
            else (pos[0] if argv[0] == "docker-compose" and pos else "")
        )
        project = option_value(argv, "-p", "--project-name") or ""
        if project.startswith("dfm-") and csub in {"down", "stop", "rm", "kill"}:
            return P(argv, A.SAFE_LOCAL, f"docker compose {csub} of DFM's own project {project}")
        if csub in {"ps", "logs", "config", "ls", "images", "top", "version", "port", "events"}:
            return P(argv, A.SAFE_READ, f"docker compose {csub}")
        if csub == "build":
            return P(argv, A.SAFE_BUILD, "docker compose build")
        if csub == "pull":
            return P(argv, A.REMOTE_READ, "docker compose pull", N.CONTAINER_REGISTRIES)
        if csub == "push":
            return P(argv, A.REMOTE_WRITE, "docker compose push", N.CONTAINER_REGISTRIES)
        if csub == "down":
            if has(argv, "-v", "--volumes", "--rmi"):
                return P(argv, A.DESTRUCTIVE, "docker compose down removes volumes/images")
            return P(argv, A.LOCAL_DESTRUCTIVE, "docker compose down removes containers")
        if csub in {"rm", "kill", "stop"}:
            return P(argv, A.LOCAL_DESTRUCTIVE, f"docker compose {csub}")
        return P(argv, A.LOCAL_STATE_CHANGE, f"docker compose {csub}")
    if (
        has(argv, "--privileged")
        or option_value(argv, "--pid") == "host"
        or (option_value(argv, "--network", "--net") == "host" and sub == "run")
    ):
        return P(argv, A.PRIVILEGED, "privileged or host-namespace container")
    for vol in option_values(argv, "-v", "--volume", "--mount"):
        if "," in vol or vol.startswith(("type=", "src=", "source=")):  # --mount key=value list
            fields: dict[str, str] = {}
            for piece in vol.split(","):
                key, _, value = piece.partition("=")
                fields[key] = value
            source = fields.get("src") or fields.get("source") or ""
        else:
            source = vol.split(":", 1)[0]
        source = source.rstrip("/") or "/"
        home_dir = re.fullmatch(r"(~|/Users/[^/]+|/home/[^/]+|/root)", source) is not None
        if (
            "docker.sock" in vol
            or home_dir
            or source in {"/", "/etc", "/Users", "/home", "/var", "/private", "/System", "/Library"}
        ):
            return P(argv, A.PRIVILEGED, f"mounts a sensitive host path ({vol})")
    if sub in {
        "ps",
        "images",
        "inspect",
        "logs",
        "version",
        "info",
        "stats",
        "top",
        "port",
        "history",
        "diff",
        "events",
        "context",
    } or (
        sub in {"image", "container", "volume", "network"}
        and pos[1:2] in (["ls"], ["inspect"], ["history"], ["list"])
    ):
        return P(argv, A.SAFE_READ, f"docker {' '.join(pos[:2])}")
    if sub in {"build", "buildx"}:
        if has(argv, "--push"):
            return P(argv, A.REMOTE_WRITE, "docker build --push", N.CONTAINER_REGISTRIES)
        return P(argv, A.SAFE_BUILD, "docker build")
    if sub == "pull":
        return P(argv, A.REMOTE_READ, "docker pull", N.CONTAINER_REGISTRIES)
    if sub in {"push"}:
        return P(argv, A.REMOTE_WRITE, "docker push", N.CONTAINER_REGISTRIES)
    if sub in {"login", "logout"}:
        return P(argv, A.PRIVILEGED, "docker registry credentials")
    if (
        sub in {"system", "volume", "image", "container", "network", "builder", "buildx"}
        and len(pos) > 1
        and pos[1] == "prune"
    ):
        filters = option_values(argv, "--filter")
        if (
            sub in {"container", "volume", "network"}
            and filters
            and all(f.startswith("label=dfm.session=") for f in filters)
        ):
            return P(argv, A.SAFE_LOCAL, f"docker {sub} prune of DFM-labelled resources only")
        return P(argv, A.DESTRUCTIVE, "docker prune deletes resources it did not create")
    if sub in {"rm", "rmi", "kill", "stop"} or (
        sub in {"volume", "network", "image", "container"}
        and len(pos) > 1
        and pos[1] in {"rm", "remove"}
    ):
        return P(argv, A.LOCAL_DESTRUCTIVE, f"docker {sub} removes/stops containers")
    if sub in {
        "run",
        "start",
        "create",
        "exec",
        "restart",
        "cp",
        "tag",
        "commit",
        "network",
        "volume",
        "pause",
        "unpause",
        "rename",
        "update",
    }:
        return P(argv, A.LOCAL_STATE_CHANGE, f"docker {sub}")
    return P(argv, A.UNKNOWN, f"no rule for docker {sub}")


def rule_kubectl(argv: list[str]) -> Part:
    pos = positional(
        argv,
        frozenset(
            {
                "-n",
                "--namespace",
                "--context",
                "-f",
                "--filename",
                "-l",
                "--selector",
                "-o",
                "--output",
                "-c",
                "--container",
                "--kubeconfig",
                "--cluster",
                "--user",
            }
        ),
    )
    sub = pos[0] if pos else ""
    ctx = option_value(argv, "--context")
    if sub == "config":
        sub2 = pos[1] if len(pos) > 1 else ""
        if sub2 == "view" and has(argv, "--raw", "--flatten"):
            return P(argv, A.PRIVILEGED, "prints kubeconfig credentials")
        if sub2 in {"view", "get-contexts", "current-context", "get-clusters", "get-users"}:
            return P(argv, A.SAFE_READ, f"kubectl config {sub2}")
        return P(
            argv,
            A.LOCAL_STATE_CHANGE,
            f"kubectl config {sub2} changes kubeconfig",
            N.KUBERNETES,
            ctx,
        )
    if sub in {
        "version",
        "explain",
        "api-resources",
        "api-versions",
        "completion",
        "plugin",
        "kustomize",
    } and not has(argv, "--server"):
        return P(
            argv,
            A.SAFE_LOCAL if sub in {"completion", "kustomize"} else A.REMOTE_READ,
            f"kubectl {sub}",
            N.KUBERNETES,
            ctx,
        )
    output = (
        option_value(argv, "-o", "--output")
        or next(  # attached: -oyaml, -o=json
            (a[2:].lstrip("=") for a in argv[1:] if a.startswith("-o") and len(a) > 2), ""
        )
    )
    resources = pos[1].split(",") if len(pos) > 1 else []  # `secrets,configmaps`
    if (
        sub == "get"
        and any(re.fullmatch(r"secrets?(/.*)?|secrets?\..*", r) for r in resources)
        and output.startswith(
            ("yaml", "json", "jsonpath", "go-template", "template", "custom-columns")
        )
    ):
        return P(argv, A.PRIVILEGED, "prints secret values", N.KUBERNETES, ctx)
    if sub in {"get", "describe", "logs", "top", "diff", "auth", "events", "wait", "cluster-info"}:
        return P(argv, A.REMOTE_READ, f"kubectl {sub} reads the cluster", N.KUBERNETES, ctx)
    if sub in {"delete", "drain", "replace"} or (
        sub == "apply" and has(argv, "--prune", "--force")
    ):
        return P(
            argv,
            A.DESTRUCTIVE_REMOTE_WRITE,
            f"kubectl {sub} can destroy cluster resources",
            N.KUBERNETES,
            ctx,
        )
    if sub in {
        "apply",
        "create",
        "patch",
        "edit",
        "scale",
        "rollout",
        "set",
        "label",
        "annotate",
        "expose",
        "run",
        "cp",
        "exec",
        "port-forward",
        "attach",
        "cordon",
        "uncordon",
        "taint",
        "autoscale",
        "debug",
        "certificate",
    }:
        return P(argv, A.REMOTE_WRITE, f"kubectl {sub} changes the cluster", N.KUBERNETES, ctx)
    return P(argv, A.UNKNOWN, f"no rule for kubectl {sub}", target=ctx)


def rule_helm(argv: list[str]) -> Part:
    pos = positional(
        argv,
        frozenset(
            {
                "-n",
                "--namespace",
                "--kube-context",
                "-f",
                "--values",
                "--set",
                "--version",
                "-o",
                "--output",
            }
        ),
    )
    sub = pos[0] if pos else ""
    sub2 = pos[1] if len(pos) > 1 else ""
    ctx = option_value(argv, "--kube-context")
    if (
        sub
        in {"template", "lint", "show", "inspect", "version", "env", "create", "package", "verify"}
        or (sub in {"dependency", "dep"} and sub2 == "list")
        or (sub == "plugin" and sub2 == "list")
        or (sub == "repo" and sub2 == "list")
    ):
        return P(argv, A.SAFE_LOCAL, f"helm {sub} is local")
    if sub in {"dependency", "dep", "pull", "fetch", "search"} or (
        sub == "repo" and sub2 in {"add", "update"}
    ):
        return P(
            argv,
            A.PACKAGE_NETWORK,
            f"helm {sub} {sub2} fetches charts".replace("  ", " "),
            N.PACKAGE_REGISTRIES,
        )
    if sub in {"list", "ls", "status", "get", "history", "hist"} or (
        has(argv, "--dry-run") and sub in {"install", "upgrade"}
    ):
        return P(argv, A.REMOTE_READ, f"helm {sub} reads the cluster", N.KUBERNETES, ctx)
    if sub in {"uninstall", "delete", "del", "un"}:
        return P(
            argv, A.DESTRUCTIVE_REMOTE_WRITE, "helm uninstall removes a release", N.KUBERNETES, ctx
        )
    if sub in {"install", "upgrade", "rollback", "test"}:
        return P(argv, A.REMOTE_WRITE, f"helm {sub} changes the cluster", N.KUBERNETES, ctx)
    if sub == "push":
        return P(argv, A.REMOTE_WRITE, "helm push", N.CONTAINER_REGISTRIES)
    return P(argv, A.UNKNOWN, f"no rule for helm {sub}")


def rule_terraform(argv: list[str]) -> Part:
    pos = positional(argv, frozenset({"-chdir", "-var", "-var-file", "-target", "-out", "-state"}))
    sub = pos[0] if pos else ""
    sub2 = pos[1] if len(pos) > 1 else ""
    if sub in {"fmt", "validate", "version", "providers", "graph", "console", "-help", "metadata"}:
        if sub == "fmt" and not has(argv, "-check", "-diff"):
            return P(argv, A.WORKTREE_WRITE, f"{argv[0]} fmt rewrites files")
        return P(argv, A.SAFE_LOCAL, f"{argv[0]} {sub}")
    if sub in {"init", "get"}:
        return P(
            argv,
            A.PACKAGE_NETWORK,
            f"{argv[0]} {sub} downloads providers/modules",
            N.PACKAGE_REGISTRIES,
        )
    if sub in {"plan", "show", "output"} or (sub == "state" and sub2 in {"list", "show", "pull"}):
        return P(argv, A.REMOTE_READ, f"{argv[0]} {sub} reads state/infrastructure")
    if (
        sub == "destroy"
        or (sub == "apply" and has(argv, "-destroy"))
        or sub == "force-unlock"
        or (sub == "state" and sub2 in {"rm", "push"})
        or (sub == "workspace" and sub2 == "delete")
    ):
        return P(argv, A.DESTRUCTIVE_REMOTE_WRITE, f"{argv[0]} {sub} can destroy infrastructure")
    if sub in {"apply", "import", "taint", "untaint", "refresh"} or (
        sub == "state" and sub2 in {"mv", "replace-provider"}
    ):
        return P(argv, A.REMOTE_WRITE, f"{argv[0]} {sub} changes infrastructure")
    if sub == "workspace":
        return P(
            argv,
            A.LOCAL_STATE_CHANGE if sub2 in {"select", "new"} else A.SAFE_LOCAL,
            f"{argv[0]} workspace {sub2}",
        )
    return P(argv, A.UNKNOWN, f"no rule for {argv[0]} {sub}")


# ---------------------------------------------------------------- databases

_SQL_DESTRUCTIVE = (
    "drop ",
    "truncate ",
    "drop\n",
    "alter table",
    "delete from",
    "dropdb",
    "flushall",
    "flushdb",
    "grant ",
    "revoke ",
)
_SQL_WRITE = (
    "insert ",
    "update ",
    "create ",
    "alter ",
    "copy ",
    "vacuum",
    "reindex",
    "merge ",
    "upsert",
    "replace into",
    "set ",
    "del ",
    "hset ",
    "lpush ",
    "rpush ",
    "expire ",
)


def _sql_class(query: str, local: bool) -> tuple[ActionClass, str]:
    q = " ".join(query.lower().split()) + " "
    if any(k in q for k in _SQL_DESTRUCTIVE):
        if (
            "delete from" in q
            and " where " in q
            and not any(k in q for k in _SQL_DESTRUCTIVE if k != "delete from")
        ):
            return (A.LOCAL_STATE_CHANGE if local else A.REMOTE_WRITE), "DELETE with WHERE"
        return A.DESTRUCTIVE if local else A.DESTRUCTIVE_REMOTE_WRITE, "destructive SQL"
    if any(q.startswith(k) or f"; {k}" in q or f";{k}" in q for k in _SQL_WRITE):
        return (A.LOCAL_STATE_CHANGE if local else A.REMOTE_WRITE), "SQL that writes"
    return (A.SAFE_READ if local else A.REMOTE_READ), "read-only SQL"


# client meta-commands that run programs, load code or touch files
_DB_META_UNSAFE = re.compile(
    r"(^|[\n;])\s*(\.(shell|system|output|once|load|import|restore|backup|save|cd)\b"
    r"|\\(!|o\b|out\b|copy\b|i\b|ir\b|include|w\b|write|g\b|gx\b|gexec)|system\b)",
    re.IGNORECASE,
)
_MONGO_DESTRUCTIVE = re.compile(
    r"\.(drop|dropDatabase|deleteOne|deleteMany|remove|findOneAndDelete)\s*\("
)
_MONGO_WRITE = re.compile(
    r"\.(insert\w*|update\w*|replaceOne|findOneAnd\w+|bulkWrite|createCollection|createIndex"
    r"|renameCollection|runCommand|adminCommand)\s*\("
)
_REDIS_EXTRA = re.compile(
    r"^\s*(config\s+set|eval|evalsha|script|shutdown|module\s+load|debug)\b", re.I
)


def _db_host(argv: list[str]) -> str | None:
    host = option_value(argv, "-h", "--host", "-H")
    if host is None:  # attached: mysql -hprod
        host = next(
            (
                a[2:]
                for a in argv[1:]
                if a.startswith("-h") and len(a) > 2 and not a.startswith("--")
            ),
            None,
        )
    for arg in argv[1:]:
        if "://" in arg:
            return host_of(arg)
        conninfo = re.search(r"\bhost(addr)?\s*=\s*'?([^\s']+)", arg)  # libpq "host=prod dbname=x"
        if conninfo:
            return conninfo.group(2)
    if host and host.startswith("/"):  # a Unix socket directory
        return None
    return host


def rule_db(argv: list[str]) -> Part:
    prog = argv[0]
    query = option_value(argv, "-c", "--command", "-e", "--execute", "--query", "-q", "--eval")
    host = _db_host(argv)
    if prog == "sqlite3":
        positional_args = positional(argv)
        query = query or (positional_args[1] if len(positional_args) > 1 else None)
        if has(argv, "-cmd", "-init"):
            query = (query or "") + "\n.shell"  # startup commands are not inspected: ask
        host = None
    if prog == "redis-cli":
        cmd = positional(argv, frozenset({"-h", "-p", "-n", "-a", "-u", "--user", "--pass"}))
        query = " ".join(cmd) if cmd else None
    local = host is None or is_loopback(host)
    network = None if local else NetworkCategory.OTHER
    if query is not None and _DB_META_UNSAFE.search(query):
        return P(
            argv, A.UNKNOWN, f"{prog} client command runs programs or touches files", network, host
        )
    if query is not None and prog in {"mongosh", "mongo"}:
        if re.search(r"\b(load|runProgram|run)\s*\(", query):
            return P(argv, A.UNKNOWN, f"{prog} script loads files or runs programs", network, host)
        if _MONGO_DESTRUCTIVE.search(query):
            return P(
                argv,
                A.DESTRUCTIVE if local else A.DESTRUCTIVE_REMOTE_WRITE,
                f"{prog}: drops or deletes data",
                network,
                host,
            )
        if _MONGO_WRITE.search(query):
            return P(
                argv,
                A.LOCAL_STATE_CHANGE if local else A.REMOTE_WRITE,
                f"{prog}: writes data",
                network,
                host,
            )
    if query is not None and prog == "redis-cli" and _REDIS_EXTRA.search(query):
        cls = A.LOCAL_STATE_CHANGE if local else A.REMOTE_WRITE
        return P(argv, cls, "redis-cli command changes server state or runs scripts", network, host)
    if query is None:
        return P(
            argv,
            A.UNKNOWN,
            f"{prog} without an inline query (interactive or file)",
            None if local else NetworkCategory.OTHER,
            host,
        )
    cls, why = _sql_class(query, local)
    return P(argv, cls, f"{prog}: {why}", None if local else NetworkCategory.OTHER, host)


# ---------------------------------------------------------------- network clients


def rule_http_client(argv: list[str]) -> Part:
    prog = argv[0]
    urls = [a for a in argv[1:] if "://" in a or (not a.startswith("-") and "." in a and "/" in a)]
    if any(u.lower().startswith("file:") for u in urls):
        return P(argv, A.UNKNOWN, f"{prog} file:// reads local files by URL")
    remote = [u for u in urls if categorize(u) is not None]
    url = remote[0] if remote else (urls[0] if urls else "")
    categories = {categorize(u) for u in remote}
    # several destinations: none of them may lend its approval to the others
    category = (
        NetworkCategory.OTHER
        if not urls or len(categories) > 1
        else (categories.pop() if categories else None)
    )
    method = (option_value(argv, "-X", "--request", "--method") or "").upper()
    sends_file = any(v.startswith("@") for v in option_values(argv, "-H", "--header"))
    writes = (
        method in {"POST", "PUT", "PATCH", "DELETE"}
        or has(
            argv,
            "-d",
            "--data",
            "--data-raw",
            "--data-binary",
            "--data-urlencode",
            "-F",
            "--form",
            "-T",
            "--upload-file",
            "--json",
            "--post-data",
            "--post-file",
        )
        or sends_file
    )
    if category is None:
        return P(argv, A.SAFE_LOCAL, f"{prog} to loopback")
    action = A.REMOTE_WRITE if writes else A.REMOTE_READ
    return P(
        argv,
        action,
        f"{prog} {'sends data to' if writes else 'reads from'} {host_of(url) or 'a host'}",
        category,
        host_of(url) or None,
    )


def rule_remote_shell(argv: list[str]) -> Part:
    host = next(
        (a for a in argv[1:] if not a.startswith("-") and ("@" in a or ":" in a or "." in a)), ""
    )
    host = host_of(host) if host else ""
    if host and is_loopback(host):
        return P(argv, A.LOCAL_STATE_CHANGE, f"{argv[0]} to localhost")
    return P(
        argv,
        A.REMOTE_WRITE,
        f"{argv[0]} runs commands or copies files on another host",
        NetworkCategory.OTHER,
        host or None,
    )


def rule_rsync(argv: list[str]) -> Part:
    if any(":" in a and not a.startswith("-") for a in argv[1:]):
        return rule_remote_shell(argv)
    if has(argv, "--delete", "--delete-after", "--delete-before", "--remove-source-files"):
        return P(argv, A.DESTRUCTIVE, "rsync --delete removes files")
    return P(argv, A.WORKTREE_WRITE, "rsync copies files locally")


def rule_brew(argv: list[str]) -> Part:
    pos = positional(argv)
    sub = pos[0] if pos else ""
    if sub in {
        "list",
        "ls",
        "info",
        "search",
        "deps",
        "uses",
        "config",
        "doctor",
        "--version",
        "outdated",
        "leaves",
        "--prefix",
    }:
        return P(argv, A.SAFE_READ, f"brew {sub}")
    if sub in {"install", "upgrade", "reinstall", "update", "tap", "fetch", "bundle"}:
        return P(argv, A.PACKAGE_NETWORK, f"brew {sub}", N.PACKAGE_REGISTRIES)
    if sub in {"uninstall", "remove", "rm", "untap", "cleanup", "autoremove"}:
        return P(argv, A.LOCAL_DESTRUCTIVE, f"brew {sub}")
    if sub == "services":
        sub2 = pos[1] if len(pos) > 1 else "list"
        if sub2 in {"list", "info"}:
            return P(argv, A.SAFE_READ, f"brew services {sub2}")
        return P(argv, A.LOCAL_STATE_CHANGE, f"brew services {sub2}")
    return P(argv, A.UNKNOWN, f"no rule for brew {sub}")


def rule_reflex(argv: list[str]) -> Part:
    sub = (positional(argv) or [""])[0]
    if sub in {
        "handbook",
        "memory",
        "skills",
        "sessions",
        "show",
        "evidence",
        "explain",
        "show-skill",
        "stats",
        "doctor",
        "agents",
        "--version",
        "--help",
        "route",
    }:
        return P(argv, A.SAFE_READ, f"reflex {sub} is read-only")
    return P(argv, A.UNKNOWN, f"reflex {sub} changes Reflex state or reads history")


def rule_repo_tools(argv: list[str]) -> Part:
    sub = (positional(argv) or [""])[0]
    if sub in {
        "--version",
        "--help",
        "audit",
        "preview",
        "diff",
        "list-targets",
        "check",
        "status",
    }:
        return P(argv, A.SAFE_READ, f"{argv[0]} {sub}")
    return P(argv, A.WORKTREE_WRITE, f"{argv[0]} {sub} writes analysis files into the repository")


RULES: dict[str, Rule] = {
    **dict.fromkeys(SAFE_READ, rule_safe_read),
    **dict.fromkeys(SAFE_READ_DEV, rule_safe_read),
    # read-only tools with options that write files or run programs
    "rg": rule_rg,
    "fd": rule_fd,
    "sort": rule_sort,
    "uniq": rule_output_operand,
    "xxd": rule_output_operand,
    "yq": rule_yq,
    "tree": rule_tree,
    "less": rule_pager,
    "more": rule_pager,
    "find": rule_find,
    "sed": rule_sed,
    "gsed": rule_sed,
    "awk": rule_awk,
    "gawk": rule_awk,
    **dict.fromkeys(
        [
            "touch",
            "mkdir",
            "cp",
            "mv",
            "ln",
            "tee",
            "patch",
            "unzip",
            "gunzip",
            "zip",
            "gzip",
            "split",
            "csplit",
        ],
        rule_write,
    ),
    "rm": rule_rm,
    "rmdir": lambda argv: P(argv, A.LOCAL_DESTRUCTIVE, "removes directories"),
    "unlink": lambda argv: P(argv, A.LOCAL_DESTRUCTIVE, "deletes a file"),
    **dict.fromkeys(
        ["shred", "dd", "mkfs", "truncate", "srm", "wipefs", "fdisk", "diskutil", "newfs_apfs"],
        rule_destructive,
    ),
    **dict.fromkeys(
        [
            "sudo",
            "doas",
            "su",
            "chown",
            "chgrp",
            "launchctl",
            "systemctl",
            "shutdown",
            "reboot",
            "halt",
            "security",
            "csrutil",
            "spctl",
            "dscl",
            "passwd",
            "visudo",
            "mount",
            "umount",
            "pfctl",
            "networksetup",
            "scutil",
            "nvram",
            "kextload",
            "tccutil",
            "pmset",
        ],
        rule_privileged,
    ),
    **dict.fromkeys(
        ["eval", "exec", "source", ".", "osascript", "expect", "script"], rule_unknown_exec
    ),
    **dict.fromkeys(["kill", "pkill", "killall"], rule_kill),
    "chmod": rule_chmod,
    "tar": rule_tar,
    "crontab": rule_crontab,
    "sysctl": rule_sysctl,
    **dict.fromkeys(["make", "gmake", "just", "task"], rule_make),
    "open": rule_open,
    "defaults": lambda argv: P(
        argv,
        A.SAFE_READ if positional(argv)[:1] in (["read"], ["domains"]) else A.LOCAL_STATE_CHANGE,
        "macOS defaults",
    ),
    "git": rule_git,
    "gh": rule_gh,
    "cargo": rule_cargo,
    "rustc": rule_safe_build,
    "rustfmt": rule_rustfmt,
    "rustup": rule_rustup,
    "python": rule_python,
    "python3": rule_python,
    "pip": rule_pip,
    "pip3": rule_pip,
    "uv": rule_uv,
    "uvx": rule_uv,
    "poetry": rule_poetry,
    **dict.fromkeys(
        [
            "pytest",
            "py.test",
            "pyright",
            "mypy",
            "tox",
            "nox",
            "coverage",
            "basedpyright",
            "pylint",
            "flake8",
            "bandit",
            "unittest",
        ],
        rule_safe_build,
    ),
    "ruff": rule_ruff,
    "black": rule_black,
    "isort": rule_check_or_fix((), ("--check", "--check-only", "-c", "--diff")),
    **dict.fromkeys(["npm", "pnpm", "yarn", "bun"], rule_node_pm),
    **dict.fromkeys(["npx", "bunx", "pnpx"], rule_npx),
    "node": rule_node,
    "deno": rule_node,
    **dict.fromkeys(
        [
            "tsc",
            "vitest",
            "jest",
            "vite",
            "next",
            "tsx",
            "ts-node",
            "mocha",
            "playwright",
            "turbo",
            "nx",
            "webpack",
            "rollup",
            "esbuild",
        ],
        rule_safe_build,
    ),
    "eslint": rule_eslint,
    "biome": rule_check_or_fix(("--write", "--apply", "--fix")),
    "prettier": rule_prettier,
    "go": rule_go,
    "gofmt": rule_gofmt,
    "golangci-lint": rule_golangci,
    "staticcheck": rule_safe_build,
    "swift": rule_swift,
    "swiftc": rule_safe_build,
    "swiftlint": rule_check_or_fix(("--fix", "--autocorrect")),
    "swift-format": rule_check_or_fix(("-i", "--in-place")),
    "xcodebuild": rule_xcodebuild,
    "xcrun": lambda argv: P(
        argv, A.SAFE_READ if has(argv, "--find", "--show-sdk-path", "-f") else A.UNKNOWN, "xcrun"
    ),
    "forge": rule_forge,
    "cast": rule_cast,
    "anvil": rule_anvil,
    "solc": rule_safe_build,
    "chisel": rule_unknown_exec,
    "scarb": rule_scarb,
    "snforge": rule_safe_build,
    "sncast": rule_starknet_cli,
    "starkli": rule_starknet_cli,
    "starknet-devnet": lambda argv: P(argv, A.LOCAL_STATE_CHANGE, "starts a local devnet"),
    **dict.fromkeys(COSMOS_DAEMONS, rule_cosmos),
    "protoc": rule_protoc,
    "buf": rule_buf,
    "sqlc": rule_sqlc,
    **dict.fromkeys(
        (
            "postgres",
            "pg_ctl",
            "initdb",
            "redis-server",
            "nats-server",
            "clickhouse",
            "clickhouse-server",
            "kafka-server-start",
            "kafka-server-start.sh",
            "mongod",
            "mysqld",
        ),
        lambda argv: P(argv, A.LOCAL_STATE_CHANGE, f"starts or initializes a local {argv[0]}"),
    ),
    "docker": rule_docker,
    "docker-compose": rule_docker,
    "podman": rule_docker,
    "kubectl": rule_kubectl,
    "helm": rule_helm,
    "terraform": rule_terraform,
    "tofu": rule_terraform,
    **dict.fromkeys(
        [
            "psql",
            "mysql",
            "sqlite3",
            "clickhouse-client",
            "clickhouse",
            "redis-cli",
            "mongosh",
            "mongo",
        ],
        rule_db,
    ),
    "pg_isready": rule_safe_read,
    "pg_dump": lambda argv: P(
        argv,
        A.SAFE_READ
        if option_value(argv, "-h", "--host") in (None, "localhost", "127.0.0.1")
        else A.REMOTE_READ,
        "pg_dump reads a database",
        None
        if option_value(argv, "-h", "--host") in (None, "localhost", "127.0.0.1")
        else NetworkCategory.OTHER,
    ),
    **dict.fromkeys(["dropdb", "dropuser"], rule_destructive),
    **dict.fromkeys(
        ["createdb", "createuser", "pg_restore"],
        lambda argv: P(argv, A.LOCAL_STATE_CHANGE, "changes a database"),
    ),
    **dict.fromkeys(["curl", "wget", "http", "https", "xh", "httpie"], rule_http_client),
    **dict.fromkeys(
        ["ssh", "scp", "sftp", "ftp", "telnet", "nc", "ncat", "netcat", "socat", "mosh"],
        rule_remote_shell,
    ),
    "rsync": rule_rsync,
    "brew": rule_brew,
    "reflex": rule_reflex,
    **dict.fromkeys(["repocanon", "repokg"], rule_repo_tools),
}
