import json
import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agents.valuation.main import PropertyValuationAgent
from agents.valuation.stages.s3_cost_execution import CostExecutionAgent
from tools.valuation.amenity_analytics_tool import get_nearby_amenities
from tools.valuation.builtup_density_tool import analyze_congestion
from tools.valuation.cbd_identification_tool import identify_cbds
from tools.valuation.data_cleaning import data_cleaning_pipeline
from tools.valuation.plot_rate_pipeline import calculate_plot_rates
from tools.valuation.factorial_table import compute_factorial_table
from tools.valuation.listing_search import listing_pipeline
from tools.valuation.db_transaction_search import fetch_db_transactions
from tools.valuation.llm_factoring_engine import run_llm_factoring
from utils.valuation.logging import RunLogger


router = APIRouter(tags=["valuation"])
logger = logging.getLogger(__name__)
valuation_agent = PropertyValuationAgent()


def _sse(event_type: str, content: Any, **kwargs: Any) -> str:
    payload = {"type": event_type, "content": content, **kwargs}
    return f"data: {json.dumps(payload, default=str)}\n\n"


@router.get("/ask_stream_valuation")
async def ask_stream(question: str, comparable_source: str = "web"):
    return StreamingResponse(
        valuation_agent.execute_stream(question, comparable_source=comparable_source),
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


# ── Transaction stream (for Internal DB comparables) ──────────────────────────
class TransactionRequest(BaseModel):
    project_id: str                 # numeric ID or project name
    property_type: str              # canonical: apartment | villa | plot | ...
    project_name: str = ""          # display name (for progress messages)


def _transaction_stream_generator(req: TransactionRequest):
    yield _sse(
        "transaction_start",
        {
            "message": f"Fetching transactions for '{req.project_name or req.project_id}' from Internal DB...",
            "project_id": req.project_id,
            "property_type": req.property_type,
        },
    )
    try:
        result = fetch_db_transactions(
            project_id=req.project_id,
            property_type=req.property_type,
        )

        if result["status"] == "success":
            yield _sse(
                "transaction_results",
                {
                    "transactions": result["transactions"],
                    "total": len(result["transactions"]),
                    "project_id": req.project_id,
                    "project_name": req.project_name or req.project_id,
                },
            )
        else:
            yield _sse(
                "transaction_no_results",
                {
                    "project_id": req.project_id,
                    "project_name": req.project_name or req.project_id,
                    "message": result.get("error", "No transactions found."),
                },
            )
    except Exception as exc:
        logger.exception("Transaction stream failed")
        yield _sse("error", f"Transaction fetch failed: {exc}")

    yield _sse("transaction_done", "Transaction fetch completed.")


@router.post("/transaction_stream")
async def transaction_stream(req: TransactionRequest):
    return StreamingResponse(_transaction_stream_generator(req), media_type="text/event-stream")


class CleaningRequest(BaseModel):
    listings: list[dict[str, Any]]          # web listings only
    subject: dict[str, Any]
    comparables: list[dict[str, Any]]
    property_type: str
    listing_type: str = "sale"
    db_transactions: list[dict[str, Any]] = []  # raw DB transactions — skip cleaning


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
            db_transactions=req.db_transactions,
            on_progress=on_progress,
        )

        # Calculate plot rates if the subject property is a plot or a villa
        if req.property_type.strip().lower() in ("plot", "villa"):
            location = req.subject.get("location_name") or req.subject.get("locality") or "Unknown"
            country = req.subject.get("country") or "India"
            result = calculate_plot_rates(
                pipeline_output=result,
                subject=req.subject,
                location=location,
                country=country,
                property_type=req.property_type,
                on_progress=on_progress,
            )

        for event in progress_events:
            yield _sse("cleaning_progress", event)

        # Define columns for the UI
        columns = [
            "cleaned_match_project",
            "cleaned_relevant_for_valuation",
            "cleaned_price_value",
            "cleaned_area_sqft",
            "cleaned_area_type",
            "cleaned_config",
            "cleaned_possession_status",
            "cleaned_listing_type",
            "final_super_builtup_area",
            "stat_flag",
        ]
        # Include plot derived columns if the subject is a plot or a villa
        if req.property_type.strip().lower() in ("plot", "villa"):
            columns.extend(
                [
                    "plot_derived_rate_per_sqft",
                    "plot_fsi_range",
                    "plot_construction_cost_range",
                    "plot_derived_rate_range",
                    "plot_derived_by",
                ]
            )

        run_logger.save_step("data_cleaning", "results", result["audit_stats"])

        cleaned_merged = result["cleaned_listings"]
        web_count = sum(1 for row in cleaned_merged if row.get("source") == "Web")
        db_count = sum(1 for row in cleaned_merged if row.get("source") == "Internal DB")

        yield _sse(
            "cleaning_results",
            {
                "cleaned_listings":  cleaned_merged,
                "review_listings":   result["review_listings"],
                "dropped_listings":  result["dropped_listings"],
                "audit_stats":       result["audit_stats"],
                "columns":           columns,
                "web_count":         web_count,
                "db_count":          db_count,
            },
        )
    except Exception as exc:
        logger.exception("Cleaning pipeline failed")
        yield _sse("error", f"Cleaning pipeline failed: {exc}")

    yield _sse("cleaning_done", "Data cleaning fetch completed.")


@router.post("/cleaning_stream")
async def cleaning_stream(req: CleaningRequest):
    return StreamingResponse(_cleaning_stream_generator(req), media_type="text/event-stream")


class RecalculatePlotRatesRequest(BaseModel):
    cleaned_listings: list[dict[str, Any]]
    subject: dict[str, Any]
    property_type: str
    overrides: dict[str, dict[str, Any]] | None = None
    fsi_override: float | None = None
    cc_override: float | None = None


def _recalculate_stream_generator(req: RecalculatePlotRatesRequest):
    yield _sse("recalculate_start", "Recalculating plot rates with user overrides...")
    
    overrides_dict = req.overrides if req.overrides is not None else {}
    pipeline_output = {"cleaned_listings": req.cleaned_listings, "audit_stats": {}}
    location = req.subject.get("location_name") or req.subject.get("locality") or "Unknown"
    country = req.subject.get("country") or "India"
    
    try:
        result = calculate_plot_rates(
            pipeline_output=pipeline_output,
            subject=req.subject,
            location=location,
            country=country,
            property_type=req.property_type,
            on_progress=None,
            overrides=overrides_dict,
            fsi_override=req.fsi_override,
            cc_override=req.cc_override
        )
        yield _sse("recalculate_results", {"listings": result["cleaned_listings"]})
    except Exception as exc:
        logger.exception("Recalculate pipeline failed")
        yield _sse("error", f"Recalculate pipeline failed: {exc}")

    yield _sse("recalculate_done", "Recalculation complete.")


@router.post("/recalculate_plot_rates_stream")
async def recalculate_plot_rates_stream(req: RecalculatePlotRatesRequest):
    return StreamingResponse(_recalculate_stream_generator(req), media_type="text/event-stream")



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
        property_type = req.subject.get("property_type", "")
        if property_type and property_type.strip().lower() == "plot":
            # Bypass LLM for plots and calculate the simple average of all cleaned listings
            table = req.factorial_data.get("table", [])
            total_rate_sum = sum(row.get("avg_rate", 0) * row.get("listing_count", 0) for row in table if row.get("avg_rate") is not None)
            total_listings = sum(row.get("listing_count", 0) for row in table if row.get("avg_rate") is not None)
            simple_avg = int(total_rate_sum / total_listings) if total_listings > 0 else 0

            currency = req.factorial_data.get("currency", "INR")
            area_unit = req.factorial_data.get("area_unit", "sqft")

            result = {
                "methodology": "Direct Average (Plot)",
                "property_type": property_type,
                "currency": currency,
                "area_unit": area_unit,
                "area_type": req.factorial_data.get("area_type", "Built-up Area"),
                "total_listing_count": req.factorial_data.get("total_valid", 0),
                "factor_table": [],
                "valuation_details": {
                    "base_rate": simple_avg,
                    "base_rate_range": {"low": simple_avg, "high": simple_avg},
                    "attribute_weights": {"neighborhood_amenity": 0, "road_type": 0, "builtup_density": 0, "cbd_score": 0},
                    "net_impacts": {"neighborhood_amenity": 0, "road_type": 0, "builtup_density": 0, "cbd_score": 0},
                    "total_net_adjustment": 0,
                    "derived_rate": simple_avg,
                    "derived_rate_range": {"low": simple_avg, "high": simple_avg},
                    "factor_breakdown": {
                        "neighborhood_amenity": {"projects": [], "subject_vs_avg": "", "net_impact": 0},
                        "road_type": {"projects": [], "subject_vs_avg": "", "net_impact": 0},
                        "builtup_density": {"projects": [], "subject_vs_avg": "", "net_impact": 0},
                        "cbd_score": {"projects": [], "subject_vs_avg": "", "net_impact": 0}
                    }
                },
                "subject_final_rate": simple_avg,
                "subject_rate_range": {"low": simple_avg, "high": simple_avg},
                "confidence": "High",
                "reasoning_audit": {
                    "stage_1_scoring_thought": "Bypassed LLM scoring for plot.",
                    "stage_2_adjustment_thought": "Bypassed adjustments. Used simple average of all cleaned listings.",
                    "final_reflection": "Calculated the global average rate directly from the cleaning table without adjustments.",
                    "key_drivers": "Direct mathematical average",
                    "uncertainties": "None"
                },
                "reconciliation_note": "For plots, the final rate is calculated directly as the simple average of all rates found in the cleaning table, without any LLM-based amenity or infrastructure adjustments.",
                "project_reports": []
            }
        else:
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


# ──────────────────────────────────────────────────────────────────────────────
# COST APPROACH — Phase 2: Apply formula after rate derivation
# ──────────────────────────────────────────────────────────────────────────────

_cost_agent = CostExecutionAgent()


class CostCalculationRequest(BaseModel):
    """
    Payload sent by the frontend after the comparable pipeline has derived the
    subject property rate (subject_final_rate from LLM factoring).

    Applicable only for: apartment, villa, retail, commercial_office
    NOT applicable for:  plot

    Simplified replacement-cost approach — only 3 user inputs required:
      - construction_rate_per_sqft  (from CPWD schedules / bank panel rates)
      - age_of_property
      - total_life_of_building      (optional, default = 60 yrs)
    """

    # ── Rate derivation output (from Phase 1 / LLM factoring) ─────────────────
    derived_rate_per_sqft: float = Field(
        ..., description="Market-derived rate per sqft for the subject property"
    )
    area_sqft: float = Field(
        ...,
        description=(
            "Salable / carpet area for apartment / retail / commercial_office; "
            "built-up area for villa — in sqft"
        ),
    )
    property_type: str = Field(
        ..., description="Canonical property type: apartment | villa | retail | commercial_office"
    )

    # ── Cost-specific user inputs ─────────────────────────────────────────────
    construction_rate_per_sqft: float = Field(
        ...,
        description=(
            "Construction cost per sqft (₹/sqft) from CPWD schedules, "
            "bank panel rates, or PWD circulars"
        ),
    )
    age_of_property: float = Field(
        ..., description="Completed age of the building in years"
    )
    total_life_of_building: float = Field(
        default=60,
        description="Expected total economic life of the building in years (default 60)",
    )


def _cost_calculation_stream_generator(req: CostCalculationRequest):
    yield _sse(
        "cost_calculation_start",
        {
            "message": "Applying Cost Approach formula...",
            "property_type": req.property_type,
            "derived_rate_per_sqft": req.derived_rate_per_sqft,
        },
    )

    try:
        result = _cost_agent.calculate_cost_value(
            derived_rate_per_sqft=req.derived_rate_per_sqft,
            area_sqft=req.area_sqft,
            property_type=req.property_type,
            construction_rate_per_sqft=req.construction_rate_per_sqft,
            age_of_property=req.age_of_property,
            total_life_of_building=req.total_life_of_building,
        )

        if not result.get("success"):
            yield _sse("error", result.get("error", "Cost calculation failed."))
        else:
            yield _sse("cost_calculation_result", result)

    except Exception as exc:
        logger.exception("Cost calculation failed")
        yield _sse("error", f"Cost calculation failed: {exc}")

    yield _sse("cost_calculation_done", "Cost Approach calculation complete.")


@router.post("/cost_calculation_stream")
async def cost_calculation_stream(req: CostCalculationRequest):
    """
    Phase 2 of the Cost Approach pipeline.

    Call this endpoint after the frontend has:
      1. Completed the market-style comparable pipeline (steps 1-6)
      2. Collected the 5 cost-specific inputs from the user

    Returns a streaming SSE response with:
      - cost_calculation_start   : confirmation echo
      - cost_calculation_result  : full result including inputs, calculations, formula audit
      - cost_calculation_done    : terminal event
      - error                    : if validation or calculation fails
    """
    return StreamingResponse(
        _cost_calculation_stream_generator(req), media_type="text/event-stream"
    )
