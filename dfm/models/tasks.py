"""Structured output schemas for small-model helpers (Apple FM first, coder as fallback).

The router uses `RouteSuggestion`; the others are schemas for helpers that are exercised by
`tests/test_structured.py` against the fm CLI's schema dialect and kept for that purpose.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from dfm.routing.routes import Route


class TaskKind(StrEnum):
    QUESTION = "question"
    EXPLAIN = "explain"
    PLAN = "plan"
    IMPLEMENT = "implement"
    FIX_TESTS = "fix_tests"
    DEBUG = "debug"
    REVIEW = "review"
    CI = "ci"
    REFACTOR = "refactor"
    ADD_TESTS = "add_tests"
    GIT_QUERY = "git_query"
    COMMIT_MESSAGE = "commit_message"
    PR_DESCRIPTION = "pr_description"
    BENCHMARK = "benchmark"
    RESUME = "resume"
    OTHER = "other"


class TaskClassification(BaseModel):
    kind: TaskKind = Field(description="What the developer is asking for")
    summary: str = Field(description="The request restated in under 20 words")
    mentioned_files: list[str] = Field(description="File paths named in the request, if any")
    needs_code_change: bool = Field(description="True if the request requires editing code")
    confidence: float = Field(description="0 to 1")


class RouteSuggestion(BaseModel):
    route: Route = Field(description="Which worker should handle the request")
    reason: str = Field(description="One sentence")
    confidence: float = Field(description="0 to 1")


class LogError(BaseModel):
    file: str | None = Field(default=None, description="Path, if the log names one")
    line: int | None = Field(default=None)
    message: str = Field(description="The error, shortened")


class CompressedLog(BaseModel):
    summary: str = Field(description="What failed, in two sentences at most")
    errors: list[LogError]
    likely_cause: str | None = None


class CommitMessage(BaseModel):
    subject: str = Field(description="Imperative mood, at most 72 characters")
    body: str | None = Field(default=None, description="Why, wrapped at 72 columns")
