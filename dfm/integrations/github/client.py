"""GitHub via `gh` (JSON output), cached under `<cache>/github/<owner__repo>/`.

Every call goes through CommandRunner, so the shell classifier and policy decide:
reads are REMOTE_READ in the `github` network category (`network.github`, default
"ask"; no TTY means no), writes such as comments are REMOTE_WRITE and always ask.
When a live read is refused or fails, the last cached copy is used and its age is
shown. gh keeps its token in its own store; DFM never reads it.

Issue and PR text, comments and check names are untrusted input: callers wrap them
with `untrusted_block` before any model sees them.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from dfm.tools.shell.runner import CommandRunner


class GitHubError(Exception):
    pass


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    author: str
    labels: list[str]
    url: str
    comments: list[str] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    state: str
    author: str
    head_ref: str
    base_ref: str
    head_sha: str
    is_draft: bool
    url: str
    files: list[str] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class ReviewComment:
    path: str | None
    line: int | None
    body: str
    author: str
    url: str = ""
    id: int | None = None


@dataclass(frozen=True)
class ReviewThread:
    id: str
    path: str | None
    line: int | None
    resolved: bool
    outdated: bool
    comments: list[ReviewComment]

    @property
    def first_comment_id(self) -> int | None:
        return self.comments[0].id if self.comments else None


@dataclass(frozen=True)
class Check:
    name: str
    state: str
    bucket: str  # pass | fail | pending | skipping | cancel
    workflow: str
    link: str

    @property
    def failed(self) -> bool:
        return self.bucket == "fail"


@dataclass(frozen=True)
class Cached[T]:
    """A value with where it came from: `live` from gh, or `cache` (with its age)."""

    value: T
    source: str
    fetched_at: float
    note: str = ""

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.fetched_at)

    def staleness(self) -> str:
        if self.source == "live":
            return "live from GitHub"
        minutes = int(self.age_s // 60)
        age = f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"
        return f"from cache, fetched {age} ago" + (f" ({self.note})" if self.note else "")


_THREADS_QUERY = (
    "query($owner: String!, $repo: String!, $number: Int!) { "
    "repository(owner: $owner, name: $repo) { pullRequest(number: $number) { "
    "reviewThreads(first: 100) { nodes { id isResolved isOutdated path line originalLine "
    "comments(first: 20) { nodes { databaseId body url author { login } } } } } } } }"
)


def repo_slug(repo: Path) -> str | None:
    """owner/name of the `origin` remote when it is on github.com (read-only git config)."""
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "remote", "get-url", "origin"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    match = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", out.stdout.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _login(raw: Any) -> str:
    return str(cast(dict[str, Any], raw or {}).get("login", "")) if isinstance(raw, dict) else ""


class GitHub:
    def __init__(self, runner: CommandRunner, slug: str, cache_root: Path) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
            raise GitHubError(f"not an owner/repo slug: {slug!r}")
        self.runner = runner
        self.slug = slug
        self.cache = cache_root / "github" / slug.replace("/", "__")

    # ---------------------------------------------------------------- plumbing

    async def _gh(self, *args: str, ok: frozenset[int] = frozenset({0})) -> str:
        outcome = await self.runner.run(["gh", *args, "-R", self.slug], timeout=60)
        if outcome.result is None:
            reason = outcome.verdicts[-1].reason if outcome.verdicts else "refused"
            raise GitHubError(f"policy: {reason}")
        if outcome.result.exit_code not in ok:
            raise GitHubError((outcome.result.stderr or outcome.result.stdout).strip()[-300:])
        return outcome.result.stdout

    async def _api(self, *args: str) -> str:
        outcome = await self.runner.run(["gh", "api", *args], timeout=60)
        if outcome.result is None:
            reason = outcome.verdicts[-1].reason if outcome.verdicts else "refused"
            raise GitHubError(f"policy: {reason}")
        if outcome.result.exit_code != 0:
            raise GitHubError((outcome.result.stderr or outcome.result.stdout).strip()[-300:])
        return outcome.result.stdout

    def _store(self, key: str, payload: Any) -> float:
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = time.time()
        path = self.cache / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": now, "data": payload}))
        tmp.replace(path)
        return now

    def _load(self, key: str) -> tuple[Any, float] | None:
        try:
            record = json.loads((self.cache / f"{key}.json").read_text())
        except (OSError, ValueError):
            return None
        return record.get("data"), float(record.get("fetched_at", 0))

    async def _fetch[T](
        self, key: str, fetch: Callable[[], Any], parse: Callable[[Any], T], *, refresh: bool
    ) -> Cached[T]:
        """Live when allowed and working; otherwise the cached copy, or GitHubError."""
        problem = ""
        if refresh:
            try:
                raw = await fetch()
            except GitHubError as exc:
                problem = str(exc)
            else:
                return Cached(parse(raw), "live", self._store(key, raw))
        cached = self._load(key)
        if cached is None:
            raise GitHubError(
                f"{key}: not cached" + (f" and GitHub unavailable: {problem}" if problem else "")
            )
        return Cached(parse(cached[0]), "cache", cached[1], problem)

    # ---------------------------------------------------------------- reads

    async def issue(self, number: int, *, refresh: bool = True) -> Cached[Issue]:
        fields = "number,title,body,state,labels,author,url,comments"

        async def fetch() -> Any:
            return json.loads(await self._gh("issue", "view", str(number), "--json", fields))

        def parse(raw: Any) -> Issue:
            d = cast(dict[str, Any], raw)
            return Issue(
                int(d["number"]),
                str(d.get("title", "")),
                str(d.get("body", "")),
                str(d.get("state", "")),
                _login(d.get("author")),
                [str(cast(dict[str, Any], label).get("name", "")) for label in d.get("labels", [])],
                str(d.get("url", "")),
                [str(cast(dict[str, Any], c).get("body", "")) for c in d.get("comments", [])],
            )

        return await self._fetch(f"issue-{number}", fetch, parse, refresh=refresh)

    async def pr(self, number: int, *, refresh: bool = True) -> Cached[PullRequest]:
        fields = (
            "number,title,body,state,author,headRefName,baseRefName,headRefOid,isDraft,url,files"
        )

        async def fetch() -> Any:
            return json.loads(await self._gh("pr", "view", str(number), "--json", fields))

        def parse(raw: Any) -> PullRequest:
            d = cast(dict[str, Any], raw)
            return PullRequest(
                int(d["number"]),
                str(d.get("title", "")),
                str(d.get("body", "")),
                str(d.get("state", "")),
                _login(d.get("author")),
                str(d.get("headRefName", "")),
                str(d.get("baseRefName", "")),
                str(d.get("headRefOid", "")),
                bool(d.get("isDraft", False)),
                str(d.get("url", "")),
                [str(cast(dict[str, Any], f).get("path", "")) for f in d.get("files", [])],
            )

        return await self._fetch(f"pr-{number}", fetch, parse, refresh=refresh)

    async def diff(self, number: int, *, refresh: bool = True) -> Cached[str]:
        async def fetch() -> Any:
            return await self._gh("pr", "diff", str(number))

        return await self._fetch(f"pr-{number}-diff", fetch, str, refresh=refresh)

    async def checks(self, number: int, *, refresh: bool = True) -> Cached[list[Check]]:
        async def fetch() -> Any:
            # gh exits 1 when checks failed and 8 while some are pending; the JSON is still valid
            try:
                out = await self._gh(
                    "pr",
                    "checks",
                    str(number),
                    "--json",
                    "name,state,bucket,workflow,link",
                    ok=frozenset({0, 1, 8}),
                )
            except GitHubError as exc:
                if "no checks" in str(exc).lower():
                    return []
                raise
            try:
                return json.loads(out or "[]")
            except ValueError as exc:
                raise GitHubError(f"gh pr checks: unexpected output: {out[:200]}") from exc

        def parse(raw: Any) -> list[Check]:
            return [
                Check(
                    str(c.get("name", "")),
                    str(c.get("state", "")),
                    str(c.get("bucket", "")),
                    str(c.get("workflow", "")),
                    str(c.get("link", "")),
                )
                for c in cast(list[dict[str, Any]], raw)
            ]

        return await self._fetch(f"pr-{number}-checks", fetch, parse, refresh=refresh)

    async def comments(self, number: int, *, refresh: bool = True) -> Cached[list[ReviewComment]]:
        """Inline review comments on a PR (file/line), newest last."""

        async def fetch() -> Any:
            return json.loads(await self._api(f"repos/{self.slug}/pulls/{number}/comments"))

        def parse(raw: Any) -> list[ReviewComment]:
            out: list[ReviewComment] = []
            for c in cast(list[dict[str, Any]], raw):
                line = c.get("line") or c.get("original_line")
                out.append(
                    ReviewComment(
                        c.get("path"),
                        int(line) if line else None,
                        str(c.get("body", "")),
                        _login(c.get("user")),
                        str(c.get("html_url", "")),
                    )
                )
            return out

        return await self._fetch(f"pr-{number}-comments", fetch, parse, refresh=refresh)

    async def review_threads(
        self, number: int, *, refresh: bool = True
    ) -> Cached[list[ReviewThread]]:
        """Review threads with resolved/outdated state (GraphQL query: a REMOTE_READ)."""
        owner, name = self.slug.split("/")

        async def fetch() -> Any:
            out = await self._api(
                "graphql",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={name}",
                "-F",
                f"number={number}",
                "-f",
                f"query={_THREADS_QUERY}",
            )
            data = cast(dict[str, Any], json.loads(out))
            pr = cast(dict[str, Any], data["data"]["repository"]["pullRequest"] or {})
            return cast(dict[str, Any], pr.get("reviewThreads", {})).get("nodes", [])

        def parse(raw: Any) -> list[ReviewThread]:
            threads: list[ReviewThread] = []
            for t in cast(list[dict[str, Any]], raw):
                line = t.get("line") or t.get("originalLine")
                comments = [
                    ReviewComment(
                        t.get("path"),
                        int(line) if line else None,
                        str(c.get("body", "")),
                        _login(c.get("author")),
                        str(c.get("url", "")),
                        c.get("databaseId"),
                    )
                    for c in cast(
                        list[dict[str, Any]],
                        cast(dict[str, Any], t.get("comments", {})).get("nodes", []),
                    )
                ]
                threads.append(
                    ReviewThread(
                        str(t.get("id", "")),
                        t.get("path"),
                        int(line) if line else None,
                        bool(t.get("isResolved")),
                        bool(t.get("isOutdated")),
                        comments,
                    )
                )
            return threads

        return await self._fetch(f"pr-{number}-threads", fetch, parse, refresh=refresh)

    async def failed_run(
        self, branch: str | None = None, *, refresh: bool = True
    ) -> Cached[dict[str, Any] | None]:
        """The newest failed workflow run (on `branch` when given), or None."""
        key = f"failed-run-{(branch or 'any').replace('/', '_')}"

        async def fetch() -> Any:
            args = ["run", "list", "--status", "failure", "--limit", "1"]
            args += ["--json", "databaseId,workflowName,displayTitle,headBranch,headSha,url"]
            if branch:
                args += ["--branch", branch]
            runs = cast(list[dict[str, Any]], json.loads(await self._gh(*args) or "[]"))
            return runs[0] if runs else None

        return await self._fetch(
            key, fetch, lambda raw: cast(dict[str, Any] | None, raw), refresh=refresh
        )

    async def run_log(self, run_id: int, *, refresh: bool = True) -> Cached[str]:
        """`gh run view <id> --log-failed`: only the failed jobs' logs."""

        async def fetch() -> Any:
            return await self._gh("run", "view", str(run_id), "--log-failed")

        return await self._fetch(f"run-{run_id}-log-failed", fetch, str, refresh=refresh)

    # ---------------------------------------------------------------- writes (always ask)

    async def reply(self, number: int, comment_id: int, body: str) -> str:
        """Reply in a review thread. REMOTE_WRITE: asks every time."""
        return await self._api(
            "-X",
            "POST",
            f"repos/{self.slug}/pulls/{number}/comments/{comment_id}/replies",
            "-f",
            f"body={body}",
        )

    async def comment(self, number: int, body: str, *, on: str = "pr") -> str:
        """Post a comment. REMOTE_WRITE: policy asks every time; refused → GitHubError."""
        kind = "pr" if on == "pr" else "issue"
        return await self._gh(kind, "comment", str(number), "--body", body)
