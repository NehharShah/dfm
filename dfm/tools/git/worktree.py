"""Isolated worktrees for large tasks and other refs.

DFM creates detached worktrees under `<data>/worktrees/<repo-hash>/` and records
each one; it removes only worktrees it created (path under its root and present
in its registry). The developer's worktrees are never pruned or removed. Results
come back as a patch or branch the developer applies; nothing merges back on its own.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from dfm.policy import ActionClass, Decision, Policy, ProposedAction
from dfm.sessions import repo_hash
from dfm.tools.errors import ToolError


@dataclass(frozen=True)
class Worktree:
    id: str
    path: Path
    base: str
    repo: Path
    session_id: str


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


class WorktreeManager:
    def __init__(
        self,
        repo: Path,
        data_dir: Path,
        policy: Policy,
        *,
        session_id: str = "",
        prefer: bool = True,
    ) -> None:
        self.repo = repo.resolve()
        self.root = data_dir / "worktrees" / repo_hash(self.repo)
        self.registry = self.root / "registry.json"
        self.policy = policy
        self.session_id = session_id
        self.prefer = prefer  # git.prefer_worktree_for_large_tasks: standing consent

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            return json.loads(self.registry.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.registry.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.registry)

    async def create(self, base: str = "HEAD") -> Worktree:
        if base.startswith("-"):
            raise ToolError(f"invalid base ref {base!r}")
        sha = _git(self.repo, "rev-parse", "--verify", "-q", f"{base}^{{commit}}")
        if sha.returncode != 0:
            raise ToolError(f"unknown ref {base!r}")
        wt_id = secrets.token_hex(4)
        path = self.root / wt_id
        cls = ActionClass.WORKTREE_WRITE if self.prefer else ActionClass.LOCAL_STATE_CHANGE
        verdict = await self.policy.authorize(
            ProposedAction(
                "git", cls, f"git worktree add --detach {path} {base}", "dfm", target=str(path)
            )
        )
        if verdict.decision is not Decision.ALLOW:
            raise ToolError(f"policy denied creating a worktree: {verdict.reason}")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        out = _git(self.repo, "worktree", "add", "--detach", "-q", str(path), sha.stdout.strip())
        if out.returncode != 0:
            raise ToolError(f"git worktree add failed: {out.stderr.strip()}")
        worktree = Worktree(wt_id, path, base, self.repo, self.session_id)
        data = self._load()
        data[wt_id] = {k: str(v) for k, v in asdict(worktree).items()}
        self._save(data)
        return worktree

    def owned(self) -> list[Worktree]:
        return [
            Worktree(k, Path(v["path"]), v["base"], Path(v["repo"]), v.get("session_id", ""))
            for k, v in self._load().items()
        ]

    def remove(self, worktree: Worktree | str) -> None:
        wt_id = worktree if isinstance(worktree, str) else worktree.id
        data = self._load()
        record = data.get(wt_id)
        if record is None:
            raise ToolError(f"worktree {wt_id} was not created by DFM; not removing it")
        path = Path(record["path"])
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ToolError(f"{path} is outside DFM's worktree root; not removing it")
        # --force: the worktree is DFM's own scratch space; its changes were already exported.
        _git(self.repo, "worktree", "remove", "--force", str(path))
        data.pop(wt_id)
        self._save(data)

    def diff(self, worktree: Worktree) -> str:
        """The worktree's changes against its base, as a patch the developer can apply."""
        _git(worktree.path, "add", "-A", "--intent-to-add")
        out = _git(worktree.path, "diff", "HEAD", "--binary")
        return out.stdout
