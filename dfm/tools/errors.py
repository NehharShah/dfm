"""Errors shared by all tools."""


class ToolError(Exception):
    """A tool refused or failed; the message is safe to show to the model."""
