import json
import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents.valuation.main import PropertyValuationAgent
from tools.valuation.amenity_analytics_tool import get_nearby_amenities
from tools.valuation.builtup_density_tool import analyze_congestion
from tools.valuation.cbd_identification_tool import identify_cbds
from tools.valuation.data_cleaning import data_cleaning_pipeline
from tools.valuation.factorial_table import compute_factorial_table
from tools.valuation.listing_search import listing_pipeline
from tools.valuation.llm_factoring_engine import run_llm_factoring
from utils.valuation.logging import RunLogger


router = APIRouter(tags=["valuation"])
logger = logging.getLogger(__name__)
valuation_agent = PropertyValuationAgent()


def _sse(event_type: str, content: Any, **kwargs: Any) -> str:
    payload = {"type": event_type, "content": content, **kwargs}
    return f"data: {json.dumps(payload, default=str)}\n\n"


@router.get("/ask_stream_valuation")
async def ask_stream_valuation(question: str):
    return StreamingResponse(
        valuation_agent.execute_stream(question),
        media_type="text/event-stream",
    )


class ListingRequest(BaseModel):
    subject: dict[str, Any]
    selected_comparables: list[dict[str, Any]]
    property_type: str
    listing_type: str = "sale"


def _listing_stream_generator(req: ListingRequest):
    yield _sse(
        "listing_start",
        {
            "message": f"Starting listing search for {len(req.selected_comparables) + 1} projects...",
            "property_type": req.property_type,
        },
    )
    progress_events: list[dict[str, Any]] = []

    def on_progress(project_name, status, detail):
        progress_events.append({"project": project_name, "status": status, "detail": detail})

    try:
        project_name = req.subject.get("project_name") or "Listing_Request"
        run_logger = RunLogger(project_name)
        run_logger.save_step("listing_search", "input_subject", req.subject)
        run_logger.save_step("listing_search", "selected_comparables", req.selected_comparables)

        result = listing_pipeline(
            subject=req.subject,
            comparables=req.selected_comparables,
            property_type=req.property_type,
            listing_type=req.listing_type,
            on_progress=on_progress,
            run_logger=run_logger,
        )

        for event in progress_events:
            yield _sse("listing_progress", event)

        yield _sse(
            "listing_results",
            {
                "listings": result["listings"],
                "total_listings": result["total_listings"],
                "projects_processed": result["projects_processed"],
                "token_usage": result["token_usage"],
            },
        )
    except Exception as exc:
        logger.exception("Listing pipeline failed")
        yield _sse("error", f"Listing pipeline failed: {exc}")

    yield _sse("listing_done", "Listing fetch completed.")


@router.post("/listing_stream")
async def listing_stream(req: ListingRequest):
    return StreamingResponse(_listing_stream_generator(req), media_type="text/event-stream")


class CleaningRequest(BaseModel):
    listings: list[dict[str, Any]]
    subject: dict[str, Any]
    comparables: list[dict[str, Any]]
    property_type: str
    listing_type: str = "sale"


def _cleaning_stream_generator(req: CleaningRequest):
    yield _sse("cleaning_start", {"message": f"Starting data cleaning for {len(req.listings)} raw listings..."})
    progress_events: list[dict[str, Any]] = []

    def on_progress(stage, detail):
        progress_events.append({"stage": stage, "detail": detail})

    try:
        project_name = req.subject.get("project_name") or "Cleaning_Request"
        run_logger = RunLogger(project_name)

        result = data_cleaning_pipeline(
            listings=req.listings,
            subject=req.subject,
            comparables=req.comparables,
            property_type=req.property_type,
            on_progress=on_progress,
        )

        for event in progress_events:
            yield _sse("cleaning_progress", event)

        run_logger.save_step("data_cleaning", "results", result["audit_stats"])
        yield _sse(
            "cleaning_results",
            {
                "cleaned_listings": result["cleaned_listings"],
                "review_listings": result["review_listings"],
                "audit_stats": result["audit_stats"],
            },
        )
    except Exception as exc:
        logger.exception("Cleaning pipeline failed")
        yield _sse("error", f"Cleaning pipeline failed: {exc}")

    yield _sse("cleaning_done", "Data cleaning fetch completed.")


@router.post("/cleaning_stream")
async def cleaning_stream(req: CleaningRequest):
    return StreamingResponse(_cleaning_stream_generator(req), media_type="text/event-stream")


class FactorialRequest(BaseModel):
    cleaned_listings: list[dict[str, Any]]
    subject: dict[str, Any]
    comparables: list[dict[str, Any]]
    currency: str = "INR"
    area_unit: str = "sqft"


def _factorial_stream_generator(req: FactorialRequest):
    yield _sse(
        "factorial_start",
        {"message": f"Computing factorial rate table for {len(req.cleaned_listings)} cleaned listings..."},
    )

    try:
        result = compute_factorial_table(
            cleaned_listings=req.cleaned_listings,
            subject=req.subject,
            comparables=req.comparables,
            currency=req.currency,
            area_unit=req.area_unit,
        )
        project_name = req.subject.get("project_name") or "Factorial_Request"
        RunLogger(project_name).save_step("factorial_table", "results", result)
        yield _sse("factorial_results", result)
    except Exception as exc:
        logger.exception("Factorial table computation failed")
        yield _sse("error", f"Factorial table computation failed: {exc}")

    yield _sse("factorial_done", "Factorial rate table generated.")


@router.post("/factorial_stream")
async def factorial_stream(req: FactorialRequest):
    return StreamingResponse(_factorial_stream_generator(req), media_type="text/event-stream")


class FactorialAnalysisRequest(BaseModel):
    factorial_data: dict[str, Any]
    subject: dict[str, Any]
    comparables: list[dict[str, Any]]
    radii: dict[str, Any] = Field(default_factory=dict)
    model: str = "gpt-4o"


def _factorial_analysis_stream_generator(req: FactorialAnalysisRequest):
    yield _sse(
        "factorial_analysis_start",
        {
            "message": (
                "Sending factorial data to GPT-4o for adjustment analysis... "
                f"({len(req.factorial_data.get('table', []))} projects; "
                f"radii: road={req.radii.get('road_m', 200)}m, "
                f"amenity={req.radii.get('amenity_m', 2000)}m, "
                f"density={req.radii.get('density_m', 500)}m)"
            )
        },
    )
    try:
        result = run_llm_factoring(
            factorial_data=req.factorial_data,
            subject=req.subject,
            comparables=req.comparables,
            radii=req.radii or None,
            model=req.model,
        )
        project_name = req.subject.get("project_name") or "Factoring_Request"
        RunLogger(project_name).save_step("llm_factoring", "results", result)
        yield _sse("factorial_analysis_result", result)
    except Exception as exc:
        logger.exception("LLM factoring failed")
        yield _sse("error", f"LLM factoring failed: {exc}")

    yield _sse("factorial_analysis_done", "LLM factorial analysis complete.")


@router.post("/factorial_analysis_stream")
async def factorial_analysis_stream(req: FactorialAnalysisRequest):
    return StreamingResponse(_factorial_analysis_stream_generator(req), media_type="text/event-stream")


class BuiltupDensityRequest(BaseModel):
    lat: float
    lng: float
    radius: float = 500.0


@router.post("/api/builtup-density")
def get_builtup_density(req: BuiltupDensityRequest):
    try:
        return analyze_congestion(req.lat, req.lng, req.radius)
    except Exception as exc:
        logger.exception("Built-up density analysis failed")
        return {"error": str(exc)}


class LocalAmenitiesRequest(BaseModel):
    lat: float
    lng: float
    radius: float = 2000.0
    city_name: str = "mumbai"


@router.post("/api/local-amenities")
def get_local_amenities_api(req: LocalAmenitiesRequest):
    try:
        amenities = get_nearby_amenities(req.lat, req.lng, req.radius, req.city_name)
        return {"amenities": amenities}
    except Exception as exc:
        logger.exception("Local amenities lookup failed")
        return {"error": str(exc)}


class CbdRequest(BaseModel):
    subject: dict[str, Any]
    comparables: list[dict[str, Any]]


@router.post("/api/cbd-identification")
def cbd_identification(req: CbdRequest):
    try:
        result = identify_cbds(subject=req.subject, comparables=req.comparables)
        return {"cbd_results": result}
    except Exception as exc:
        logger.exception("CBD identification failed")
        return {"error": str(exc)}
