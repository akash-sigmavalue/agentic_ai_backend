from fastapi import APIRouter

from registry.portfolio.registry import get_section, list_sections, upload_sections

router = APIRouter(tags=["sections"])


@router.get("/sections")
def sections():
    return list_sections()


@router.get("/sections/uploadable")
def uploadable_sections():
    return upload_sections()


@router.get("/sections/{section_key}/fields")
def section_fields(section_key: str):
    return get_section(section_key)["fields"]
