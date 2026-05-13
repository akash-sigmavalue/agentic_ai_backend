from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests

from core.web_search.config import config


DOCUMENT_PATTERN = re.compile(
    r"\.(?:pdf|docx?|xlsx?|pptx?|txt)(?:$|\?|#)",
    re.IGNORECASE,
)


class DocumentDownloader:
    """Download and extract text from documents discovered during crawling."""

    def __init__(
        self,
        download_dir: str = None,
        max_downloads: int = None,
        max_bytes: int = None,
    ):
        self.download_dir = Path(download_dir or config.DOCUMENT_DOWNLOAD_DIR)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_downloads = config.DOCUMENT_MAX_DOWNLOADS if max_downloads is None else max_downloads
        self.max_bytes = config.DOCUMENT_MAX_BYTES if max_bytes is None else max_bytes

    async def find_and_download_documents(self, crawler_results: List[Dict]) -> List[Dict]:
        document_urls = []
        source_by_url = {}

        for page in crawler_results:
            source_url = page.get("url", "")
            for url in page.get("document_links", []) or []:
                if self._is_document_url(url):
                    document_urls.append(url)
                    source_by_url.setdefault(url, source_url)

            for url in re.findall(r"https?://[^\s<>\"')]+", page.get("content", "")):
                if self._is_document_url(url):
                    document_urls.append(url)
                    source_by_url.setdefault(url, source_url)

        documents = []
        for url in list(dict.fromkeys(document_urls))[: self.max_downloads]:
            doc_info = await self._download_and_extract(url, source_by_url.get(url, ""))
            if doc_info:
                documents.append(doc_info)
        return documents

    async def _download_and_extract(self, url: str, source_url: str) -> Optional[Dict]:
        def download() -> Optional[Dict]:
            try:
                response = requests.get(
                    url,
                    timeout=30,
                    stream=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; AgenticSearchBot/1.0)"},
                    allow_redirects=True,
                )
                if response.status_code != 200:
                    return None

                content_length = int(response.headers.get("content-length") or 0)
                if content_length and content_length > self.max_bytes:
                    return None

                data = response.content
                if len(data) > self.max_bytes:
                    return None

                filename = self._safe_filename(url)
                filepath = self.download_dir / filename
                filepath.write_bytes(data)
                text = self._extract_text(filepath)

                return {
                    "url": url,
                    "filename": filename,
                    "filepath": str(filepath),
                    "source_url": source_url,
                    "size": len(data),
                    "content": text[: config.MAX_CONTENT_LENGTH],
                    "content_type": filepath.suffix.lower().lstrip(".") or "document",
                }
            except Exception as exc:
                print(f"Error downloading document {url}: {exc}")
                return None

        return await asyncio.to_thread(download)

    def _extract_text(self, filepath: Path) -> str:
        suffix = filepath.suffix.lower()
        try:
            if suffix == ".pdf":
                return self._extract_pdf_text(filepath)
            if suffix in {".doc", ".docx"}:
                return self._extract_docx_text(filepath)
            if suffix in {".xls", ".xlsx"}:
                return self._extract_excel_text(filepath)
            if suffix == ".txt":
                return filepath.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            print(f"Error extracting document {filepath}: {exc}")
        return ""

    def _extract_pdf_text(self, filepath: Path) -> str:
        try:
            import fitz

            with fitz.open(filepath) as doc:
                return "\n".join(page.get_text("text") for page in doc[:10]).strip()
        except Exception:
            from pypdf import PdfReader

            reader = PdfReader(str(filepath))
            return "\n".join((page.extract_text() or "") for page in reader.pages[:10]).strip()

    def _extract_docx_text(self, filepath: Path) -> str:
        import docx2txt

        return docx2txt.process(str(filepath)) or ""

    def _extract_excel_text(self, filepath: Path) -> str:
        import pandas as pd

        sheets = pd.read_excel(filepath, sheet_name=None, nrows=100)
        parts = []
        for name, frame in sheets.items():
            parts.append(f"Sheet: {name}\n{frame.to_csv(index=False)}")
        return "\n\n".join(parts)

    def _safe_filename(self, url: str) -> str:
        parsed = urlparse(url)
        raw_name = Path(parsed.path).name
        if not raw_name or "." not in raw_name:
            ext = ".pdf" if ".pdf" in url.lower() else ".bin"
            raw_name = f"document_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}{ext}"
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name)[:160]
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
        path = Path(stem)
        return f"{path.stem}_{digest}{path.suffix}"

    def _is_document_url(self, url: str) -> bool:
        return bool(DOCUMENT_PATTERN.search(url or ""))
