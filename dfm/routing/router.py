"""Pick a route for a request: deterministic rules first, Apple FM when unsure.

Routes ending in a coder also carry a tier (`small` / `primary`). Adjusters run
after the decision (resource manager downgrade, Reflex evidence) and may only
move between coder tiers or add reasons, never turn an edit into TOOLS_ONLY.
A route is not a permission: every action a route leads to still goes through policy.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from dfm.models.base import GenerateRequest, Model, ModelError
from dfm.models.structured import structured
from dfm.routing.routes import Route

Tier = Literal["small", "primary"]
Source = Literal["deterministic", "apple", "reflex", "override", "fallback", "resources"]

CODER_ROUTES = {
    Route.SMALL_CODER,
    Route.PRIMARY_CODER,
    Route.APPLE_THEN_CODER,
    Route.TOOLS_THEN_CODER,
    Route.CODER_THEN_APPLE,
}


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reasons: tuple[str, ...]
    source: Source
    confidence: float
    coder: Tier | None = None

    def with_coder(self, tier: Tier, reason: str, source: Source) -> "RouteDecision":
        route = self.route
        if route in {Route.SMALL_CODER, Route.PRIMARY_CODER}:
            route = Route.SMALL_CODER if tier == "small" else Route.PRIMARY_CODER
        return replace(
            self, route=route, coder=tier, reasons=(*self.reasons, reason), source=source
        )


@dataclass(frozen=True)
class RoutingContext:
    override: Route | None = None
    override_coder: Tier | None = None
    failing_tests: bool = False
    diff_lines: int = 0
    languages: tuple[str, ...] = ()
    apple_available: bool = True
    extra: dict[str, str] = field(default_factory=lambda: {})


Adjuster = Callable[[RouteDecision, str, RoutingContext], RouteDecision]

# Apple FM fits ~8k tokens; a diff above this many lines goes to a coder instead.
APPLE_DIFF_LINE_LIMIT = 600


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(p, text) for p in patterns)


_RESUME = r"^\s*resume\b|\bwhat were we doing\b|\bpick up where\b|\bcontinue (where|what) (we|i) (left|were)\b"


def is_resume_request(text: str) -> bool:
    """'resume what we were doing yesterday' and friends: handled by `dfm resume`."""
    return re.search(_RESUME, text.lower()) is not None


_TOOLS_ONLY = (
    r"^\s*git\s+(status|log|diff|branch|show|blame|remote|stash list|tag)\b",
    r"^\s*(what|which)\s+(branch|commit)\b",
    r"\bcurrent branch\b",
    r"^\s*(show|list)\s+(me\s+)?(the\s+)?(status|diff|changes|branches|recent commits|commits|sessions|processes|services)\b",
    r"^\s*what('s| is| has)?\s+changed\b",
    r"^\s*(run|rerun|re-run)\s+(the\s+)?(tests?|test suite|pytest|cargo test|go test|npm test|pnpm test|linter|lint|build|typecheck)\b",
    r"^\s*(where|in which file)\s+(is|are)\s+\S+\s+(defined|declared|implemented|used)\b",
    r"\b(find|list|show)\s+(all\s+)?(usages|references|callers)\s+of\b",
    r"^\s*(dfm\s+)?(doctor|status)\s*$",
    _RESUME,
)

_APPLE_TEXT = (
    r"\b(write|draft|generate|suggest)\s+(a\s+|the\s+)?commit( message)?\b",
    r"\bcommit message\b",
    r"\b(pr|pull request)\s+(description|summary|title)\b",
    r"\b(summari[sz]e|summary of)\s+(the\s+|my\s+|this\s+)?(diff|changes|commits|branch)\b",
    r"\bchangelog\b|\brelease notes\b",
    r"\b(draft|write)\s+(an?\s+)?issue\b",
)
_REPO_EXPLAIN = (
    r"\bexplain\s+(this|the)\s+(repo|repository|codebase|project)\b",
    r"\bwhat\s+(does|is)\s+this\s+(repo|repository|codebase|project)\b",
    r"\b(overview|architecture)\s+of\s+(this|the)\s+(repo|repository|codebase|project)\b",
    r"\bhow\s+is\s+(this|the)\s+(repo|repository|codebase|project)\s+(structured|organized|laid out)\b",
    r"\bhow do i (build|test|run|lint)\s+(this|the)\s+(repo|repository|project)\b",
)
_EDIT_VERBS = (
    r"\b(fix|implement|add|create|write|refactor|rename|change|update|remove|delete|migrate|port|"
    r"rewrite|resolve|make|support|introduce|extract|replace|optimi[sz]e|speed up|upgrade|bump|"
    r"handle|patch|repair|address|clean ?up)\b"
)
_INVESTIGATE = r"\b(debug|investigate|diagnose|why\s+(is|does|do|did)|figure out|root cause)\b"
_REVIEW = r"\b(review|audit|check)\s+(the\s+|my\s+|this\s+|current\s+)?(diff|changes|pr|pull request|code|branch|commit)\b|\bcode review\b|^\s*review\b"
_CI = r"\b(ci|pipeline|workflow|github actions|build is (red|failing|broken))\b"
_TESTS = r"\b(failing|broken|red|flaky)\s+(tests?|specs?|suite)\b|\btests?\s+(are\s+|is\s+)?(failing|broken)\b|\b(test\s+)?failures\b|\bmake\s+(the\s+)?tests?\s+pass\b"
_ISSUE = r"\b(issue|ticket)\s*#?\d+|#\d+\b"
_SMALL = (
    r"\btypo\b",
    r"\b(one|single)[- ]line\b",
    r"\b(small|tiny|trivial|simple|quick|minor)\b",
    r"\brename\s+(the\s+)?(variable|function|field|parameter|method|arg)\b",
    r"\b(docstring|comment|readme|log message|error message|wording)\b",
    r"\bbump\s+(the\s+)?version\b",
    r"\badd\s+(a\s+)?(test|unit test|type hint|annotation)\b",
    r"\bformat(ting)?\b",
)
_PRIMARY = (
    r"\b(refactor|architecture|architectural|redesign|re-architect|subsystem|migrate|migration|across)\b",
    r"\b(race|deadlock|concurren|thread[- ]safe|memory leak|use[- ]after|lifetime|borrow checker|unsafe)\b",
    r"\b(security|vulnerab|auth[a-z]*|permission|crypto)\b",
    r"\b(performance|optimi[sz]e|benchmark|latency)\b",
    r"\b(compiler error|type error|trait bound|generic|macro)\b",
    r"\b(implement|build)\s+(the\s+)?(feature|issue|ticket|endpoint|api|service|module)\b",
)
_HARD_LANGUAGES = {"rust", "cairo", "solidity", "swift", "c++"}


class Router:
    def __init__(
        self,
        helper: Model | None = None,
        *,
        adjusters: Sequence[Adjuster] = (),
        on_decision: Callable[[RouteDecision], None] | None = None,
        min_apple_confidence: float = 0.5,
    ) -> None:
        self._helper = helper
        self._adjusters = list(adjusters)
        self._on_decision = on_decision
        self._min_conf = min_apple_confidence

    def add_adjuster(self, adjuster: Adjuster) -> None:
        self._adjusters.append(adjuster)

    def deterministic(self, request: str, ctx: RoutingContext) -> RouteDecision | None:
        text = request.strip().lower()
        if ctx.override is not None:
            tier = ctx.override_coder or (
                "small" if ctx.override is Route.SMALL_CODER else "primary"
            )
            coder = tier if ctx.override in CODER_ROUTES else None
            return RouteDecision(ctx.override, ("user override",), "override", 1.0, coder)

        wants_change = _has(text, _EDIT_VERBS, _INVESTIGATE, _ISSUE) and not _has(
            text, r"^\s*git\s"
        )
        if _has(text, *_TOOLS_ONLY) and not wants_change:
            return RouteDecision(Route.TOOLS_ONLY, ("answerable by tools",), "deterministic", 0.9)
        if _has(text, *_APPLE_TEXT):
            if ctx.diff_lines > APPLE_DIFF_LINE_LIMIT:
                return RouteDecision(
                    Route.SMALL_CODER,
                    ("short text task", f"diff of {ctx.diff_lines} lines exceeds Apple FM context"),
                    "deterministic",
                    0.8,
                    "small",
                )
            route = Route.APPLE if ctx.apple_available else Route.SMALL_CODER
            return RouteDecision(
                route,
                ("short text task",),
                "deterministic",
                0.9,
                None if route is Route.APPLE else "small",
            )
        if _has(text, *_REPO_EXPLAIN):
            route = Route.APPLE if ctx.apple_available else Route.TOOLS_THEN_CODER
            reasons = ("repository explanation from RepoCanon + RepoKG facts",)
            return RouteDecision(
                route, reasons, "deterministic", 0.85, None if route is Route.APPLE else "small"
            )
        if _has(text, _REVIEW):
            return RouteDecision(
                Route.TOOLS_THEN_CODER,
                ("review: tools gather the diff",),
                "deterministic",
                0.85,
                "primary",
            )

        ci = _has(text, _CI) and _has(text, r"\b(fix|failing|red|broken|green|repair)\b")
        tests = ctx.failing_tests or _has(text, _TESTS)
        investigate = _has(text, _INVESTIGATE)
        edit = _has(text, _EDIT_VERBS) or _has(text, _ISSUE)
        if not (ci or tests or investigate or edit):
            return None

        tier = self._tier(text, ctx, ci=ci, investigate=investigate)
        reasons = [f"code task ({tier} coder)"]
        if ci or tests:
            reasons.append("tools reproduce the failure first")
            route = Route.TOOLS_THEN_CODER
        elif investigate:
            reasons.append("tools gather evidence first")
            route = Route.TOOLS_THEN_CODER
        elif ctx.apple_available and (len(request) > 400 or _has(text, _ISSUE)):
            reasons.append("Apple FM structures the requirements first")
            route = Route.APPLE_THEN_CODER
        else:
            route = Route.SMALL_CODER if tier == "small" else Route.PRIMARY_CODER
        return RouteDecision(route, tuple(reasons), "deterministic", 0.8, tier)

    @staticmethod
    def _tier(text: str, ctx: RoutingContext, *, ci: bool, investigate: bool) -> Tier:
        if _has(text, *_PRIMARY) or ci or investigate or ctx.diff_lines > 300:
            return "primary"
        if ctx.failing_tests and _HARD_LANGUAGES & {lang.lower() for lang in ctx.languages}:
            return "primary"
        if _has(text, *_SMALL):
            return "small"
        if len(re.findall(r"[\w/.-]+\.[a-z]{1,5}\b", text)) > 3:
            return "primary"
        return "primary" if _has(text, _ISSUE) else "small"

    async def _ask_apple(self, request: str) -> RouteDecision | None:
        if self._helper is None:
            return None
        from dfm.models.tasks import RouteSuggestion  # tasks imports Route from this package

        prompt = (
            "Choose how a local engineering agent should handle this developer request.\n"
            "TOOLS_ONLY: answered by running read-only tools (git, search). APPLE: short text "
            "writing or summary, no code reasoning. SMALL_CODER: simple code edit. PRIMARY_CODER: "
            "complex code work. APPLE_THEN_CODER: vague request that needs structuring before "
            "coding. TOOLS_THEN_CODER: needs facts from tools (tests, logs) before coding. "
            "CODER_THEN_APPLE: code work followed by a written summary.\n\n"
            f"Request: {request[:2000]}"
        )
        try:
            suggestion = await structured(
                self._helper,
                GenerateRequest.prompt(prompt, purpose="classify", timeout_s=20),
                RouteSuggestion,
            )
        except ModelError:
            return None
        if suggestion.confidence < self._min_conf:
            return None
        coder: Tier | None = None
        if suggestion.route in CODER_ROUTES:
            coder = "primary" if suggestion.route is Route.PRIMARY_CODER else "small"
        return RouteDecision(
            suggestion.route,
            (f"Apple FM: {suggestion.reason}",),
            "apple",
            suggestion.confidence,
            coder,
        )

    async def route(self, request: str, ctx: RoutingContext | None = None) -> RouteDecision:
        ctx = ctx or RoutingContext()
        decision = self.deterministic(request, ctx)
        if decision is None and ctx.apple_available:
            decision = await self._ask_apple(request)
        if decision is None:
            decision = RouteDecision(
                Route.TOOLS_THEN_CODER,
                ("no rule matched; gather context, then the small coder answers",),
                "fallback",
                0.4,
                "small",
            )
        if decision.source != "override":
            for adjust in self._adjusters:
                adjusted = adjust(decision, request, ctx)
                if decision.route in CODER_ROUTES and adjusted.route not in CODER_ROUTES:
                    continue  # adjusters may not turn code work into a non-coding route
                decision = adjusted
        if self._on_decision is not None:
            self._on_decision(decision)
        return decision
