"""`dfm benchmark`: ablation benchmarks over fixture tasks."""

from dfm.benchmark.runner import (
    ABLATIONS,
    OUTCOMES,
    ReferenceCoder,
    RunResult,
    classify_outcome,
    integrity_violations,
    run_suite,
    run_task,
    table,
    write_results,
)
from dfm.benchmark.tasks import BY_ID, TASKS, BenchTask

__all__ = [
    "ABLATIONS",
    "BY_ID",
    "OUTCOMES",
    "TASKS",
    "BenchTask",
    "ReferenceCoder",
    "RunResult",
    "classify_outcome",
    "integrity_violations",
    "run_suite",
    "run_task",
    "table",
    "write_results",
]
