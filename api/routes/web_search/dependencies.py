"""Shared API dependencies."""

from typing import Optional


_search_agent: Optional[object] = None


def get_search_agent():
    """Return the process-wide search agent instance."""
    global _search_agent

    if _search_agent is None:
        from agents.web_search.main import DuckDuckGoSearchAgent

        _search_agent = DuckDuckGoSearchAgent()

    return _search_agent
