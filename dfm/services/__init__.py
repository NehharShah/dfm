"""Local service lifecycle (detect, start, healthcheck, stop-if-owned)."""

from dfm.services.catalog import Detection, ServiceSpec, detect
from dfm.services.manager import ServiceError, ServiceHandle, ServiceManager, port_open

__all__ = [
    "Detection",
    "ServiceError",
    "ServiceHandle",
    "ServiceManager",
    "ServiceSpec",
    "detect",
    "port_open",
]
