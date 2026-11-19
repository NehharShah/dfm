"""Route selection: deterministic rules, Apple FM assist, resource adjustment."""

from dfm.routing.router import (
    CODER_ROUTES,
    Adjuster,
    RouteDecision,
    Router,
    RoutingContext,
    Tier,
    is_resume_request,
)
from dfm.routing.routes import EDITING_INTENTS, Intent, Route

__all__ = [
    "CODER_ROUTES",
    "EDITING_INTENTS",
    "Adjuster",
    "Intent",
    "Route",
    "RouteDecision",
    "Router",
    "RoutingContext",
    "Tier",
    "is_resume_request",
]
