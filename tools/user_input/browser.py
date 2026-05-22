import base64
import json
import os
from typing import List
import opendataloader_pdf

import fitz
from fastapi import HTTPException
from langchain_community.document_loaders import Docx2txtLoader
from langchain_core.documents import Document

from core.user_input.config import OCR_AVAILABLE, POPPLER_PATH, convert_from_path, pytesseract
from database.user_input_runtime import runtime
from utils.user_input.helpers import is_table_like
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from core.user_input.config import OPENAI_API_KEY


def extract_images_from_pdf(file_path):
    doc = fitz.open(file_path)
    image_docs = []

    for page_index in range(len(doc)):
        page = doc[page_index]
        images = page.get_images(full=True)

        for image_index, image in enumerate(images):
            xref = image[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            image_ext = base_image.get("ext", "png")
            encoded = base64.b64encode(image_bytes).decode("utf-8")

            image_docs.append(
                Document(
                    page_content="[IMAGE]",
                    metadata={
                        "page": page_index + 1,
                        "image_base64": encoded,
                        "image_mime": f"image/{image_ext}",
                        "image_index": image_index,
                        "type": "image",
                    },
                )
            )

    return image_docs


def load_pdf_with_ocr(file_path: str) -> List[Document]:
    if not OCR_AVAILABLE:
        raise HTTPException(status_code=500, detail="OCR libraries not installed")

    try:
        images = convert_from_path(file_path, dpi=300, poppler_path=POPPLER_PATH if POPPLER_PATH else None)
        documents = []
        for index, image in enumerate(images):
            text = pytesseract.image_to_string(image)
            if text.strip():
                documents.append(Document(page_content=text, metadata={"page": index + 1, "source": "ocr"}))
        return documents
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"OCR failed: {exc}")


def load_pdf_with_opendataloader(file_path: str) -> List[Document]:

    result = opendataloader_pdf.convert(file_path, format="markdown,json")

    if isinstance(result, str):
        try:
            data = json.loads(result)
        except Exception:
            data = []
    else:
        data = result

    docs = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "elements" in data:
        items = data["elements"]
    elif isinstance(data, dict) and "pages" in data:
        items = data["pages"]
    elif isinstance(data, dict):
        items = [data]
    else:
        items = []

    for item in items:
        md_content = item.get("markdown") or item.get("text") or item.get("content") or ""
        if not md_content:
            continue

        page_num = item.get("page", item.get("page_number", 1))

        docs.append(
            Document(
                page_content=md_content,
                metadata={
                    "page": page_num,
                    "source": "opendataloader",
                    "type": "text",
                    "section": item.get("section_id") or item.get("section"),
                    "title": item.get("section_title") or item.get("title"),
                    "bbox": item.get("bbox") or item.get("bounding_box"),
                    "is_table": is_table_like(md_content),
                },
            )
        )

    return docs


def load_documents(file_path: str, filename: str) -> List[Document]:
    extension = os.path.splitext(filename)[1].lower()

    if extension in [".png", ".jpg", ".jpeg", ".webp", ".bmp"]:
        runtime.loader_type = "image"
        with open(file_path, "rb") as f:
            image_bytes = f.read()
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        mime_type = f"image/{extension.replace('.', '')}"
        if mime_type == "image/jpg":
            mime_type = "image/jpeg"

        runtime.page_images[(filename, 1)] = [
            Document(
                page_content="[IMAGE]",
                metadata={
                    "source": filename,
                    "page": 1,
                    "image_base64": encoded,
                    "image_mime": mime_type,
                    "image_index": 0,
                    "type": "image",
                }
            )
        ]

        description = ""
        try:
            if OPENAI_API_KEY:
                llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=OPENAI_API_KEY)
                message = HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": (
                                "You are an expert technical document transcriber.\n"
                                "Transcribe all text, numbers, and labels from this image with absolute precision.\n"
                                "Pay extreme attention to:\n"
                                "1. Exact numbers and decimal points. Do NOT add, remove, or shift decimal points.\n"
                                "2. Exact units of measurement (e.g., 'mm', 'm', 'cm'). Do NOT convert units (e.g., do NOT change '150 mm' to '1.50 m').\n"
                                "3. Verify every label carefully before transcribing.\n"
                                "Format your output in clean Markdown."
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded}"}
                        }
                    ]
                )
                res = llm.invoke([message])
                description = res.content.strip()
        except Exception as exc:
            print(f"VLM image description failed, falling back: {exc}")

        if not description and OCR_AVAILABLE:
            try:
                from PIL import Image
                description = pytesseract.image_to_string(Image.open(file_path))
            except Exception as exc:
                print(f"OCR fallback failed: {exc}")

        if not description:
            description = f"[Uploaded image: {filename}. Could not extract text content automatically.]"

        return [
            Document(
                page_content=description,
                metadata={
                    "page": 1,
                    "source": filename,
                    "type": "text",
                    "chunk_type": "image_description",
                }
            )
        ]

    if extension == ".pdf":
        # Priority 1: OpenDataLoader (richest structured extraction — markdown, sections, tables, bbox)
        try:
            docs = load_pdf_with_opendataloader(file_path)
            total_text = " ".join(doc.page_content for doc in docs)
            if len(total_text.strip()) > 500:
                runtime.loader_type = "opendataloader"
                return docs
        except Exception as exc:
            print(f"OpenDataLoader failed, falling back to PyPDFLoader: {exc}")

        # Priority 2: PyPDFLoader (fast text extraction fallback)
        try:
            from langchain_community.document_loaders import PyPDFLoader

            loader = PyPDFLoader(file_path)
            docs = loader.load()

            for doc in docs:
                page = doc.metadata.get("page")
                if isinstance(page, int):
                    doc.metadata["page"] = page + 1

            total_text = " ".join(doc.page_content for doc in docs)
            if len(total_text.strip()) > 500:
                runtime.loader_type = "pypdf"
                return docs

        except Exception as exc:
            print(f"PyPDFLoader failed, falling back to OCR: {exc}")

        # Priority 3: OCR (last resort for scanned/image-based PDFs)
        runtime.loader_type = "ocr"
        return load_pdf_with_ocr(file_path)

    if extension == ".docx":
        runtime.loader_type = "docx"
        return Docx2txtLoader(file_path).load()

    raise HTTPException(status_code=400, detail="Only PDF, DOCX, and image files (.png, .jpg, .jpeg, .webp, .bmp) are supported.")
