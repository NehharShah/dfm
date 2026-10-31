"""GitHub through the `gh` CLI, with a local cache for offline work."""

from dfm.integrations.github.client import (
    Cached,
    Check,
    GitHub,
    GitHubError,
    Issue,
    PullRequest,
    ReviewComment,
    ReviewThread,
    repo_slug,
)

__all__ = [
    "Cached",
    "Check",
    "GitHub",
    "GitHubError",
    "Issue",
    "PullRequest",
    "ReviewComment",
    "ReviewThread",
    "repo_slug",
]
