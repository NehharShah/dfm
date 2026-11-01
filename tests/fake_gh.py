#!/usr/bin/env python3
"""A stand-in for `gh` serving tests/fixtures/github (shapes captured from real gh output).

Settings come from `fake_gh.json` next to the script (DFM scrubs unknown environment
variables from child processes): fixtures dir, log file (one JSON argv list per line),
optional pr_sha (overrides headRefOid), pr_diff (file with the PR diff), run_log, and
checks_mode: "failed" (default; exit 1 like gh when a check failed), "pending" (exit 8),
"none" (gh's "no checks reported" error), "garbage" (non-JSON stdout) or "hang".

The fake rejects what real gh rejects on the surface DFM uses: a missing `-R owner/repo`,
a `--json` with a field the fixture does not have (gh: "Unknown JSON field"), `run list`
without its filters, `run view` without `--log-failed`. A production change that drifts from
real gh then fails here instead of only against GitHub.
"""

import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
settings = json.loads((Path(__file__).parent / "fake_gh.json").read_text())
with Path(settings["log"]).open("a") as log:
    log.write(json.dumps(args) + "\n")
fixtures = Path(settings["fixtures"])
words = [a for a in args if not a.startswith("-")]


def flag(name: str) -> str | None:
    if name in args:
        at = args.index(name)
        return args[at + 1] if at + 1 < len(args) else ""
    return next((a.split("=", 1)[1] for a in args if a.startswith(name + "=")), None)


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    sys.exit(code)


def load(name: str) -> object:
    path = fixtures / name
    if not path.exists():
        fail(f"GraphQL: Could not resolve to an issue or pull request ({name})")
    return json.loads(path.read_text())


def emit(name: str, *, exit_code: int = 0) -> None:
    data = load(name)
    requested = flag("--json")
    if requested is None:
        fail("fake gh: `--json <fields>` is required here (gh would print a table instead)", 2)
    sample = data[0] if isinstance(data, list) and data else data
    if isinstance(sample, dict) and requested is not None:
        unknown = [f for f in requested.split(",") if f and f not in sample]
        if unknown:
            fail(f"Unknown JSON field: {unknown[0]!r}\nAvailable fields:\n  " + "\n  ".join(sample))
    if (
        isinstance(data, dict)
        and name.startswith("pr_")
        and "headRefOid" in data
        and settings.get("pr_sha")
    ):
        data["headRefOid"] = settings["pr_sha"]
    print(json.dumps(data))
    sys.exit(exit_code)


if words[:1] != ["api"] and flag("-R") is None and flag("--repo") is None:
    fail("fake gh: `-R owner/repo` is required (the repository cannot be inferred here)", 2)

if words[:2] == ["issue", "view"]:
    emit(f"issue_{words[2]}.json")
elif words[:2] == ["pr", "view"]:
    emit(f"pr_{words[2]}.json")
elif words[:2] == ["pr", "checks"]:
    mode = settings.get("checks_mode", "failed")
    if mode == "hang":
        time.sleep(120)
    if mode == "none":
        fail(f"no checks reported on the 'feature' branch (pr {words[2]})")
    if mode == "garbage":
        print("<html>rate limited</html>")
        sys.exit(0)
    emit(f"pr_{words[2]}_checks.json", exit_code=8 if mode == "pending" else 1)
elif words[:2] == ["pr", "diff"]:
    print(Path(settings["pr_diff"]).read_text(), end="")
elif words[:2] == ["api", "graphql"]:
    number = next(w.split("=", 1)[1] for w in words if w.startswith("number="))
    nodes = json.loads((fixtures / f"pr_{number}_threads.json").read_text())
    print(
        json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}})
    )
elif words[:1] == ["api"] and words[1].endswith("/replies"):
    print(json.dumps({"id": 1, "body": "posted"}))
elif words[:1] == ["api"] and words[1].endswith("/comments"):
    data = load(f"pr_{words[1].split('/')[-2]}_review_comments.json")
    print(json.dumps(data))
elif words[:2] == ["run", "list"]:
    if flag("--status") != "failure" or flag("--limit") is None:
        fail("fake gh: `run list` is expected with `--status failure --limit N`", 2)
    emit("runs.json")
elif words[:2] == ["run", "view"]:
    if "--log-failed" not in args:
        fail("fake gh: `run view` is expected with `--log-failed`", 2)
    print(Path(settings["run_log"]).read_text(), end="")
elif words[1:2] == ["comment"]:
    if flag("--body") is None:
        fail("fake gh: `comment` needs `--body`", 2)
    print(f"https://github.com/acme/widget/pull/{words[2]}#issuecomment-1")
else:
    fail(f"fake gh: unsupported {args}", 2)
