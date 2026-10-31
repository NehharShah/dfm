"""Route selection: deterministic rules, Apple FM assist, Reflex evidence."""

from dfm.routing.router import (
    CODER_ROUTES,
    Adjuster,
    RouteDecision,
    Router,
    RoutingContext,
    Tier,
    is_resume_request,
)
from dfm.routing.routes import Route

__all__ = [
    "CODER_ROUTES",
    "Adjuster",
    "Route",
    "RouteDecision",
    "Router",
    "RoutingContext",
    "Tier",
    "is_resume_request",
]
