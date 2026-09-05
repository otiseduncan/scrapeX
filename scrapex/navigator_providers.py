"""Provider adapter boundary for the Navigator.

A provider encodes exactly the site-specific knowledge the generic
observation/action/graph layers must not: what "target selected" looks like,
what counts as a search action, and how to score relevance. Everything else
-- session, observation, actions, graph, verification shape -- is shared.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol


class NavigatorProvider(Protocol):
    slug: str
    home_url: str
    allowed_domain_suffixes: tuple[str, ...]

    async def authenticated(self, page: Any) -> bool:
        """Best-effort check that the persistent profile is still logged in."""
        ...

    async def target_signal(self, page: Any, target: dict[str, Any]) -> dict[str, Any]:
        """Bounded UI signal for whether ``target`` (e.g. a vehicle) is selected.

        Must return ``{"selected": bool, "reason": str | None, ...}`` -- never
        a substring match against the whole page, which will match almost
        anything by luck (a year picker, a "recent vehicles" list, etc).
        """
        ...

    def is_search_action(self, action: dict[str, Any]) -> bool:
        """Whether one executed action counts as "a query was submitted"."""
        ...

    def match_terms(self, text: str, topic: str) -> tuple[list[str], int]:
        """Return (matched terms, relevance score) for ``text`` against ``topic``."""
        ...


def domain_allowed(url: str, allowed_domain_suffixes: tuple[str, ...]) -> bool:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").casefold()
    if not host:
        return False
    return any(
        host == suffix.casefold() or host.endswith("." + suffix.casefold())
        for suffix in allowed_domain_suffixes
    )
