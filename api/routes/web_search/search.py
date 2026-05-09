"""Search, extraction, and statistics endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query

from api.routes.web_search.dependencies import get_search_agent
from core.web_search.config import config


router = APIRouter()


@router.get("/search")
async def search(
    query: str = Query(..., min_length=1),
    max_results: int = config.MAX_RESULTS_IN_RESPONSE,
    no_cache: bool = False,
    agent=Depends(get_search_agent),
):
    """Search the internet and return analyzed results."""
    return agent.search(query, max_results, use_cache=not no_cache)


@router.get("/extract")
async def extract(
    url: str = Query(..., min_length=5),
    query: str = Query(..., min_length=1),
    agent=Depends(get_search_agent),
):
    """Extract the requested information from a specific URL."""
    result = agent.extract_from_url(url, query)
    if result.get("success"):
        return result

    raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))


@router.get("/stats")
async def get_stats(agent=Depends(get_search_agent)):
    """Return search agent statistics."""
    return agent.get_stats()
