import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import get_settings

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper())
static_dir = Path(__file__).parent / "static"

app = FastAPI(title=settings.app_name, version="0.1.0")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.include_router(router)


@app.get("/", include_in_schema=False)
async def frontend() -> FileResponse:
    return FileResponse(static_dir / "index.html")
