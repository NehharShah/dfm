#!/usr/bin/env python3
"""A stand-in for `gh` serving tests/fixtures/github (shapes captured from real gh output).

Settings come from `fake_gh.json` next to the script (DFM scrubs unknown environment
variables from child processes): fixtures dir, log file (one JSON argv list per line),
optional pr_sha (overrides headRefOid) and pr_diff (file with the PR diff).
"""

import json
import sys
from pathlib import Path

args = sys.argv[1:]
settings = json.loads((Path(__file__).parent / "fake_gh.json").read_text())
with Path(settings["log"]).open("a") as log:
    log.write(json.dumps(args) + "\n")
fixtures = Path(settings["fixtures"])
words = [a for a in args if not a.startswith("-")]


def emit(name: str) -> None:
    path = fixtures / name
    if not path.exists():
        print(f"GraphQL: Could not resolve to an issue or pull request ({name})", file=sys.stderr)
        sys.exit(1)
    data = json.loads(path.read_text())
    if name.startswith("pr_") and "headRefOid" in data and settings.get("pr_sha"):
        data["headRefOid"] = settings["pr_sha"]
    print(json.dumps(data))


if words[:2] == ["issue", "view"]:
    emit(f"issue_{words[2]}.json")
elif words[:2] == ["pr", "view"]:
    emit(f"pr_{words[2]}.json")
elif words[:2] == ["pr", "checks"]:
    emit(f"pr_{words[2]}_checks.json")
    sys.exit(1)  # like gh: non-zero when a check failed
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
    emit(f"pr_{words[1].split('/')[-2]}_review_comments.json")
elif words[:2] == ["run", "list"]:
    emit("runs.json")
elif words[:2] == ["run", "view"]:
    print(Path(settings["run_log"]).read_text(), end="")
elif words[1:2] == ["comment"]:
    print(f"https://github.com/acme/widget/pull/{words[2]}#issuecomment-1")
else:
    print(f"fake gh: unsupported {args}", file=sys.stderr)
    sys.exit(2)
