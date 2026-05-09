"""FastAPI application entry point.

This module owns application setup: app metadata, middleware, router
registration, startup jobs, and legacy root/health endpoints.
"""

import asyncio
import io
import logging
import os
import sys
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRouter
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from agents.connector.services.gmail_pubsub import GmailPubSubService
from agents.connector.super_agent import SuperAgent
from agents.data_retrieval.pipeline import UniversalRealEstateAgent
from api.routes.connector import connectors, debug, gmail_webhook, oauth
from api.routes.geospatial import map_overlays, maps
from api.routes.ui_creation import generation
from api.routes.user_input import user_input
from api.routes.valuation.valuation import router as valuation_router
from api.routes.web_search import chat as web_search_chat
from api.routes.web_search import health as web_search_health
from api.routes.web_search import search as web_search_search
from api.routes.web_search import web as web_search_web
from api.schemas.connector.request_models import WorkflowRequest
from api.schemas.connector.workflow_models import WorkflowExecutionResult
# Authentication/authorization is currently disabled.
# from api.routes.connector import auth as connector_auth
# from auth.connector.dependencies import get_optional_current_user
from core.config import settings
from core.connector.logging import configure_logging
from core.web_search.config import config as web_search_config
from database.connector import models as connector_models  # noqa: F401
from database.db import Base as ConnectorBase, Base, engine, SessionLocal, get_db
from database.db import engine as connector_engine
from database.connector.schema_migration import ensure_additive_schema


if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

configure_logging()
logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")
data_retrieval_agent = UniversalRealEstateAgent()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        description="Real Estate Intelligence Agent API",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _include_routes(app)
    _mount_frontend(app)
    return app


def _include_routes(app: FastAPI) -> None:
    # Authentication/authorization routes are unlinked while auth is disabled.
    # app.include_router(connector_auth.router)
    app.include_router(connectors.router)
    app.include_router(debug.router)
    app.include_router(gmail_webhook.router)
    app.include_router(oauth.router)
    app.include_router(maps.router)
    app.include_router(map_overlays.router)
    app.include_router(generation.router)
    app.include_router(user_input.router, prefix="/user-input", tags=["user-input"])
    app.include_router(valuation_router)
    app.include_router(web_search_search.router, prefix="/api", tags=["web-search"])
    app.include_router(web_search_chat.router, prefix="/api", tags=["web-search"])
    app.include_router(web_search_health.router, prefix="/api")
    app.include_router(web_search_web.router, prefix="/web-search")
    app.include_router(_build_workflow_router())


def _build_workflow_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["workflow"])

    def get_super_agent() -> SuperAgent:
        return SuperAgent()

    @router.post("/process")
    async def process_workflow(
        payload: WorkflowRequest,
        db: Session = Depends(get_db),
        # Authentication/authorization is disabled.
        # current_user=Depends(get_optional_current_user),
        super_agent: SuperAgent = Depends(get_super_agent),
    ) -> dict[str, Any]:
        try:
            result: WorkflowExecutionResult = await super_agent.handle_request(
                payload,
                db=db,
                current_user=None,
            )
        except Exception as exc:
            logger.exception("Workflow execution failed")
            return {"success": False, "error": str(exc), "failed_stage": "workflow", "failed_step": None}
        return result.model_dump(exclude_none=True)

    @router.get("/health")
    async def workflow_health() -> dict[str, str]:
        return {"status": "ok"}

    return router


def _mount_frontend(app: FastAPI) -> None:
    frontend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
    if os.path.isdir(frontend_dir):
        app.mount("/ui", StaticFiles(directory=frontend_dir, html=True), name="frontend")


def _renew_gmail_watch_job() -> None:
    async def run_renewal() -> None:
        db = SessionLocal()
        try:
            await GmailPubSubService().renew_expiring_watches(db)
        finally:
            db.close()

    try:
        asyncio.run(run_renewal())
    except Exception:
        logger.exception("Failed to renew Gmail watches")


app = create_app()


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    ConnectorBase.metadata.create_all(bind=connector_engine)
    ensure_additive_schema(connector_engine)
    if not scheduler.running:
        scheduler.add_job(
            _renew_gmail_watch_job,
            CronTrigger(minute="*/30"),
            id="gmail_watch_renewal",
            replace_existing=True,
        )
        scheduler.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/")
def read_root():
    return {
        "message": f"Welcome to the {settings.PROJECT_NAME} API",
        "docs": "/docs",
        "agents": ["data_retrieval", "valuation", "geospatial", "connector", "ui_creation"],
    }


@app.get("/health")
def health_check():
    return {"status": "ok", "service": settings.PROJECT_NAME}


@app.get("/ask_stream_data_retrieval")
async def ask_stream_data_retrieval(
    question: str,
    selected_domain: str | None = None,
    session_id: str | None = None,
):
    return StreamingResponse(
        data_retrieval_agent.execute_stream(
            question,
            selected_domain=selected_domain,
            session_id=session_id,
        ),
        media_type="text/event-stream",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=web_search_config.API_HOST, port=web_search_config.API_PORT)
