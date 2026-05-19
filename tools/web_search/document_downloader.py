from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests

from core.web_search.config import config
from database.web_search.cache import SearchCache


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
        self.cache = SearchCache() if config.CACHE_ENABLED else None

    async def find_and_download_documents(self, crawler_results: List[Dict], query_context: str = "") -> List[Dict]:
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
        queued_urls = self._prioritize_documents(list(dict.fromkeys(document_urls)), query_context)
        seen_urls = set()
        index = 0

        while index < len(queued_urls) and len(documents) < self.max_downloads:
            url = queued_urls[index]
            index += 1
            if url in seen_urls:
                continue
            seen_urls.add(url)

            cached = self._get_cached_document(url, source_by_url.get(url, ""))
            if cached:
                documents.append(cached)
                new_links = []
                for linked_url in self._extract_links_from_document(cached.get("content", "")):
                    if linked_url not in seen_urls and self._is_document_url(linked_url):
                        source_by_url.setdefault(linked_url, url)
                        new_links.append(linked_url)
                queued_urls.extend(self._prioritize_documents(list(dict.fromkeys(new_links)), query_context))
                continue

            doc_info = await self._download_and_extract(url, source_by_url.get(url, ""))
            if doc_info:
                self._cache_document(url, doc_info)
                documents.append(doc_info)
                new_links = []
                for linked_url in self._extract_links_from_document(doc_info.get("content", "")):
                    if linked_url not in seen_urls and self._is_document_url(linked_url):
                        source_by_url.setdefault(linked_url, url)
                        new_links.append(linked_url)
                queued_urls.extend(self._prioritize_documents(list(dict.fromkeys(new_links)), query_context))
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
                    "reference_urls": self._reference_urls(url, source_url),
                    "extraction_metadata": {
                        "document_url": url,
                        "source_page_url": source_url,
                        "reference_urls": self._reference_urls(url, source_url),
                        "content_type": filepath.suffix.lower().lstrip(".") or "document",
                        "size": len(data),
                    },
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

    def _get_cached_document(self, url: str, source_url: str) -> Optional[Dict]:
        if not self.cache:
            return None

        cached = self.cache.get(url, search_type="document")
        if not cached:
            return None

        filepath = cached.get("filepath")
        if filepath and not Path(filepath).exists():
            return None

        doc_info = dict(cached)
        if source_url:
            doc_info["source_url"] = source_url
        doc_info["reference_urls"] = self._reference_urls(doc_info.get("url", url), doc_info.get("source_url", ""))
        extraction_metadata = dict(doc_info.get("extraction_metadata") or {})
        extraction_metadata.setdefault("document_url", doc_info.get("url", url))
        extraction_metadata.setdefault("source_page_url", doc_info.get("source_url", ""))
        extraction_metadata["reference_urls"] = doc_info["reference_urls"]
        doc_info["extraction_metadata"] = extraction_metadata
        print(f"Document cache hit for: {url}")
        return doc_info

    def _cache_document(self, url: str, doc_info: Dict) -> None:
        if not self.cache:
            return
        self.cache.set(url, doc_info, search_type="document")

    def _prioritize_documents(self, document_links: List[str], query_context: str) -> List[str]:
        if not document_links:
            return []

        query_terms = {
            word.lower()
            for word in re.findall(r"[A-Za-z0-9]+", query_context or "")
            if len(word) > 1
        }
        years = re.findall(r"\b20\d{2}\b", query_context or "")
        scored_docs = []
        for index, doc_url in enumerate(document_links):
            url_lower = doc_url.lower()
            score = 0
            extension_match = re.search(r"\.([a-z0-9]+)(?:$|\?|#)", url_lower)
            extension = extension_match.group(1) if extension_match else ""

            if extension in query_terms:
                score += 30
            if "excel" in query_terms and extension in {"xls", "xlsx"}:
                score += 30
            if "pdf" in query_terms and extension == "pdf":
                score += 30
            if any(year in url_lower for year in years):
                score += 20
            for term in query_terms:
                if term in url_lower:
                    score += 10
            if any(term in url_lower for term in ["official", "easr", "asr", "rate", "rates", "valuation", "reckoner", "circle", "guideline"]):
                score += 15

            scored_docs.append((score, -index, doc_url))

        scored_docs.sort(reverse=True)
        return [doc_url for _score, _index, doc_url in scored_docs]

    def _extract_links_from_document(self, document_content: str) -> List[str]:
        if not document_content:
            return []

        url_pattern = r"https?://[^\s<>\"{}|\\^`\[\]]+"
        urls = []
        for raw_url in re.findall(url_pattern, document_content):
            url = raw_url.rstrip(").,;:'\"")
            if self._is_document_url(url):
                urls.append(url)
        return list(dict.fromkeys(urls))

    def _reference_urls(self, document_url: str, source_url: str = "") -> List[str]:
        return list(dict.fromkeys(url for url in [document_url, source_url] if url))
