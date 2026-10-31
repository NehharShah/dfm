"""The routes a request can take."""

from enum import StrEnum


class Route(StrEnum):
    TOOLS_ONLY = "TOOLS_ONLY"  # deterministic tools answer it; no model
    APPLE = "APPLE"  # Apple FM alone (short text work)
    SMALL_CODER = "SMALL_CODER"  # dfm-small
    PRIMARY_CODER = "PRIMARY_CODER"  # dfm-primary
    APPLE_THEN_CODER = "APPLE_THEN_CODER"  # Apple FM structures, coder does the work
    TOOLS_THEN_CODER = "TOOLS_THEN_CODER"  # tools gather facts, coder reasons
    CODER_THEN_APPLE = "CODER_THEN_APPLE"  # coder works, Apple FM summarizes
