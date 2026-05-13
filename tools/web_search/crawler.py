from __future__ import annotations

import asyncio
import re
from typing import Dict, List, Optional, Set
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from core.web_search.config import config


DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt")


class WebCrawler:
    """Small same-domain crawler used to enrich top search results."""

    def __init__(
        self,
        max_depth: int = None,
        max_pages: int = None,
        timeout: int = None,
    ):
        self.max_depth = config.CRAWL_MAX_DEPTH if max_depth is None else max_depth
        self.max_pages = config.CRAWL_MAX_PAGES if max_pages is None else max_pages
        self.timeout = config.CRAWL_TIMEOUT if timeout is None else timeout
        self.visited: Set[str] = set()
        self.results: List[Dict] = []

    async def crawl(self, start_url: str, query_context: str = "") -> List[Dict]:
        self.visited = set()
        self.results = []
        await self._crawl_recursive(self._normalize_url(start_url), 0, query_context or "")
        return self.results

    async def crawl_many(self, start_urls: List[str], query_context: str = "") -> List[Dict]:
        self.visited = set()
        self.results = []
        for url in start_urls:
            if len(self.results) >= self.max_pages:
                break
            await self._crawl_recursive(self._normalize_url(url), 0, query_context or "")
        return self.results

    async def _crawl_recursive(self, url: str, depth: int, query_context: str) -> None:
        if depth > self.max_depth or len(self.results) >= self.max_pages:
            return
        if not url or url in self.visited or self._is_document_url(url):
            return

        self.visited.add(url)
        html = await self._fetch_page(url)
        if not html:
            return

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        links, document_links = self._extract_links(soup, url)
        text_content = self._extract_text(soup)

        self.results.append({
            "url": url,
            "depth": depth,
            "title": title,
            "content": text_content[: config.MAX_CONTENT_LENGTH],
            "links": links[:20],
            "document_links": document_links[:20],
        })

        relevant_links = [link for link in links if self._is_relevant_link(link, query_context)]
        for link in relevant_links[:8]:
            if len(self.results) >= self.max_pages:
                break
            await self._crawl_recursive(link, depth + 1, query_context)

    async def _fetch_page(self, url: str) -> Optional[str]:
        def fetch() -> Optional[str]:
            try:
                response = requests.get(
                    url,
                    timeout=self.timeout,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; AgenticSearchBot/1.0)"},
                    allow_redirects=True,
                )
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code == 200 and "text/html" in content_type:
                    response.encoding = response.apparent_encoding or "utf-8"
                    return response.text
            except Exception as exc:
                print(f"Error crawling {url}: {exc}")
            return None

        return await asyncio.to_thread(fetch)

    def _extract_text(self, soup: BeautifulSoup) -> str:
        soup = BeautifulSoup(str(soup), "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        return re.sub(r"\n{2,}", "\n", text).strip()

    def _extract_links(self, soup: BeautifulSoup, base_url: str) -> tuple[List[str], List[str]]:
        links = []
        document_links = []
        base_domain = urlparse(base_url).netloc.lower()

        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:")):
                continue

            full_url = self._normalize_url(urljoin(base_url, href))
            parsed = urlparse(full_url)
            if parsed.scheme not in {"http", "https"}:
                continue
            if parsed.netloc.lower() != base_domain:
                if self._is_document_url(full_url):
                    document_links.append(full_url)
                continue
            if self._is_binary_asset(full_url):
                continue
            if self._is_document_url(full_url):
                document_links.append(full_url)
            else:
                links.append(full_url)

        return self._dedupe(links), self._dedupe(document_links)

    def _is_relevant_link(self, url: str, query_context: str) -> bool:
        if not query_context:
            return True
        if len(self.results) < 5:
            return True

        url_text = urlparse(url).path.lower().replace("-", " ").replace("_", " ")
        keywords = [
            word.lower()
            for word in re.findall(r"[A-Za-z0-9]+", query_context)
            if len(word) > 2
        ]
        score = sum(1 for keyword in keywords if keyword in url_text)
        relevance_terms = [
            "about", "project", "property", "listing", "download", "document",
            "pdf", "rates", "research", "report", "news", "data", "details",
        ]
        score += sum(1 for term in relevance_terms if term in url_text)
        return score > 0

    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        clean_url, _fragment = urldefrag(url)
        return clean_url.rstrip("/")

    def _is_document_url(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return path.endswith(DOCUMENT_EXTENSIONS)

    def _is_binary_asset(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".rar", ".mp4", ".mp3"))

    def _dedupe(self, values: List[str]) -> List[str]:
        return list(dict.fromkeys(values))


class LLMGuidedCrawler(WebCrawler):
    """Optional crawler that asks an LLM whether a link is worth following."""

    def __init__(self, llm_client, max_depth: int = None, max_pages: int = None, timeout: int = None):
        super().__init__(max_depth=max_depth, max_pages=max_pages, timeout=timeout)
        self.llm_client = llm_client

    def _is_relevant_link(self, url: str, query_context: str) -> bool:
        if not self.llm_client or len(self.visited) < 5:
            return super()._is_relevant_link(url, query_context)

        prompt = (
            "Given this search query/context, decide if this URL is likely to contain "
            "specific useful information for answering it.\n\n"
            f"Query: {query_context}\n"
            f"URL: {url}\n\n"
            "Respond with only YES or NO."
        )
        try:
            response = self.llm_client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0,
            )
            return "YES" in (response.choices[0].message.content or "").upper()
        except Exception:
            return super()._is_relevant_link(url, query_context)
