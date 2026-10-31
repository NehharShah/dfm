"""Provider-neutral Model protocol and local model adapters."""

from dfm.models.base import (
    ContextLimitExceeded,
    GenerateRequest,
    GenerateResult,
    Message,
    Model,
    ModelError,
    ModelHealth,
    ModelIncompatible,
    ModelRefusal,
    ModelTimeout,
    ModelUnavailable,
    RemoteModelRefused,
    StructuredOutputError,
)

__all__ = [
    "ContextLimitExceeded",
    "GenerateRequest",
    "GenerateResult",
    "Message",
    "Model",
    "ModelError",
    "ModelHealth",
    "ModelIncompatible",
    "ModelRefusal",
    "ModelTimeout",
    "ModelUnavailable",
    "RemoteModelRefused",
    "StructuredOutputError",
]
