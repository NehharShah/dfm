"""GitHub Actions workflows → jobs → steps → the shell commands they run.

Only `run:` steps become local commands. Lines that use `${{ ... }}` expressions,
or steps that need a matrix value, cannot be reproduced literally and are reported
as such. Setup actions (`uses:`) are listed but never run: their local equivalent
is the developer's installed toolchain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml


@dataclass(frozen=True)
class Step:
    name: str  # as GitHub shows it: the step's name, else "Run <first line>" / "Run <action>"
    run: str | None
    uses: str | None
    working_directory: str | None

    def commands(self) -> list[str]:
        """The run script as separate commands (continuations joined, comments dropped)."""
        if not self.run:
            return []
        joined = re.sub(r"\s*\\\n\s*", " ", self.run)
        lines = [ln.strip() for ln in joined.splitlines()]
        return [ln for ln in lines if ln and not ln.startswith("#")]

    @property
    def reproducible(self) -> bool:
        return bool(self.run) and "${{" not in (self.run or "")


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    steps: list[Step] = field(default_factory=lambda: [])
    working_directory: str | None = None


@dataclass(frozen=True)
class Workflow:
    path: str
    name: str
    jobs: list[Job]


def _step(raw: dict[str, Any], default_dir: str | None) -> Step:
    run = cast(str | None, raw.get("run"))
    uses = cast(str | None, raw.get("uses"))
    name = raw.get("name")
    if not name:
        name = f"Run {run.strip().splitlines()[0]}" if run else f"Run {uses}"
    return Step(str(name), run, uses, cast(str | None, raw.get("working-directory")) or default_dir)


def load_workflows(repo: Path) -> list[Workflow]:
    out: list[Workflow] = []
    directory = repo / ".github" / "workflows"
    for path in sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")]):
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        doc = cast(dict[str, Any], data)
        jobs: list[Job] = []
        for job_id, raw_job in cast(dict[str, Any], doc.get("jobs") or {}).items():
            if not isinstance(raw_job, dict):
                continue
            job = cast(dict[str, Any], raw_job)
            defaults = cast(
                dict[str, Any], cast(dict[str, Any], job.get("defaults") or {}).get("run") or {}
            )
            default_dir = cast(str | None, defaults.get("working-directory"))
            steps = [
                _step(cast(dict[str, Any], s), default_dir)
                for s in cast(list[Any], job.get("steps") or [])
                if isinstance(s, dict)
            ]
            jobs.append(Job(str(job_id), str(job.get("name") or job_id), steps, default_dir))
        out.append(
            Workflow(path.relative_to(repo).as_posix(), str(doc.get("name") or path.stem), jobs)
        )
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def match_step(
    workflows: list[Workflow], job_name: str, step_title: str
) -> tuple[Job, Step] | None:
    """The workflow step a log's failing `##[group]Run …` block belongs to.

    Log job names may carry a matrix suffix ("test (3.12)") or be the job id. The group
    title (after "Run ") is the first line of the step's script, or the action it uses,
    even when the step has a name; a name is accepted too.
    """
    wanted = _norm(step_title.removeprefix("Run "))
    base_job = _norm(re.sub(r"\s*\(.*\)$", "", job_name.split(" / ")[-1]))
    best: tuple[Job, Step] | None = None
    for workflow in workflows:
        for job in workflow.jobs:
            job_matches = base_job in {_norm(job.id), _norm(re.sub(r"\s*\$\{\{.*", "", job.name))}
            for step in job.steps:
                first = _norm(step.run.strip().splitlines()[0].rstrip("\\")) if step.run else ""
                names = {_norm(step.name), _norm(step.name.removeprefix("Run "))}
                if step.uses:
                    names.add(_norm(step.uses))
                if wanted in names or (first and (wanted == first or wanted.startswith(first))):
                    if job_matches:
                        return job, step
                    best = best or (job, step)
    return best
