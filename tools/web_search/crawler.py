from __future__ import annotations

import asyncio
import re
from typing import Dict, List, Optional, Set
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from core.web_search.config import config
from database.web_search.cache import SearchCache


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
        self.pagination_seen: Dict[str, int] = {}
        self.crawl_cache = SearchCache() if config.CACHE_ENABLED else None

    async def crawl(self, start_url: str, query_context: str = "") -> List[Dict]:
        self.visited = set()
        self.results = []
        self.pagination_seen = {}
        normalized = self._normalize_url(start_url)
        if self._is_document_url(normalized):
            self._record_document_start(normalized)
        else:
            await self._crawl_recursive(normalized, 0, query_context or "")
        return self.results

    async def crawl_many(self, start_urls: List[str], query_context: str = "") -> List[Dict]:
        self.visited = set()
        self.results = []
        self.pagination_seen = {}

        normalized_urls = [self._normalize_url(url) for url in start_urls if url]
        document_priority = self._prioritize_documents(
            [url for url in normalized_urls if self._is_document_url(url)],
            query_context,
        )
        html_queue = [url for url in normalized_urls if not self._is_document_url(url)]

        for url in document_priority:
            if len(self.results) >= self.max_pages:
                break
            self._record_document_start(url)

        for url in html_queue:
            if len(self.results) >= self.max_pages:
                break
            found_answer = await self._crawl_recursive(url, 0, query_context or "")
            if found_answer:
                break
        return self.results

    async def _crawl_recursive(self, url: str, depth: int, query_context: str) -> bool:
        if depth > self.max_depth or len(self.results) >= self.max_pages:
            return False
        if not url or url in self.visited or self._is_document_url(url):
            return False
        if not self._should_follow_link(url, depth):
            return False

        self.visited.add(url)
        cached_result = self._get_cached_page(url)
        if cached_result:
            self.results.append(cached_result)
            if self._has_strong_content_evidence(cached_result, query_context):
                return True
            if depth < self.max_depth:
                relevant_links = self._rank_links_by_query(
                    [
                        link for link in cached_result.get("links", [])
                        if self._is_relevant_link(link, query_context, depth + 1)
                    ],
                    query_context,
                )
                for link in relevant_links:
                    if len(self.results) >= self.max_pages:
                        break
                    found_answer = await self._crawl_recursive(link, depth + 1, query_context)
                    if found_answer:
                        return True
            return False

        html = await self._fetch_page(url)
        if not html:
            return False

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        links, document_links = self._extract_links(soup, url, query_context)
        text_content = self._extract_text(soup)
        evidence_score = self._content_evidence_score(text_content, title, url, query_context)

        result = {
            "url": url,
            "depth": depth,
            "title": title,
            "content": text_content[: config.MAX_CONTENT_LENGTH],
            "links": self._rank_links_by_query(links, query_context)[:50],
            "document_links": document_links[:50],
            "evidence_score": evidence_score,
            "has_strong_evidence": evidence_score >= 0.72,
        }
        self.results.append(result)
        self._cache_page(url, result)

        if result["has_strong_evidence"]:
            return True

        relevant_links = self._rank_links_by_query(
            [link for link in links if self._is_relevant_link(link, query_context, depth + 1)],
            query_context,
        )
        for link in relevant_links:
            if len(self.results) >= self.max_pages:
                break
            found_answer = await self._crawl_recursive(link, depth + 1, query_context)
            if found_answer:
                return True

        return False

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

    def _extract_links(self, soup: BeautifulSoup, base_url: str, query_context: str = "") -> tuple[List[str], List[str]]:
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

        return self._dedupe(links), self._prioritize_documents(self._dedupe(document_links), query_context)

    def _is_relevant_link(self, url: str, query_context: str, depth: int = 0) -> bool:
        if not query_context:
            return True
        if len(self.results) < 5:
            return depth == 0 or self._has_strong_signal(url) or not self._is_pagination_link(url)

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
        if depth <= 0:
            return score > 0

        required_score = 2 if depth == 1 else 3
        return score >= required_score or (score > 0 and self._has_strong_signal(url))

    def _rank_links_by_query(self, links: List[str], query_context: str) -> List[str]:
        scored = [
            (self._link_relevance_score(link, query_context), -index, link)
            for index, link in enumerate(self._dedupe(links))
        ]
        scored.sort(reverse=True)
        return [link for _score, _index, link in scored]

    def _link_relevance_score(self, url: str, query_context: str) -> float:
        url_text = self._normalize_digits(
            f"{urlparse(url).path} {urlparse(url).query}"
        ).lower().replace("-", " ").replace("_", " ")
        query_terms = self._important_query_terms(query_context)
        constraints = self._extract_query_constraints(query_context)

        score = 0.0
        if any(term in url_text for term in query_terms):
            score += sum(1 for term in query_terms if term in url_text) / max(len(query_terms), 1)
        if constraints:
            score += 0.4 * sum(1 for item in constraints if item.lower() in url_text) / len(constraints)
        if self._has_strong_signal(url):
            score += 0.35
        if self._is_pagination_link(url):
            score -= 0.25
        return score

    def _has_strong_content_evidence(self, result: Dict, query_context: str) -> bool:
        if result.get("has_strong_evidence"):
            return True
        content = result.get("content") or ""
        title = result.get("title") or ""
        url = result.get("url") or ""
        return self._content_evidence_score(content, title, url, query_context) >= 0.72

    _INDIC_DIGIT_TRANSLATION = str.maketrans({
        "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
        "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    })

    def _normalize_digits(self, text: str) -> str:
        return str(text or "").translate(self._INDIC_DIGIT_TRANSLATION)

    def _content_evidence_score(self, content: str, title: str, url: str, query_context: str) -> float:
        if not query_context or not content:
            return 0.0

        haystack = self._normalize_digits(f"{title}\n{url}\n{content}").lower()
        query = self._normalize_digits(query_context).lower()
        query_terms = self._important_query_terms(query)
        constraints = self._extract_query_constraints(query)

        score = 0.0
        if len(query) >= 8 and query in haystack:
            score += 0.30

        if constraints:
            matched_constraints = sum(
                1 for item in constraints
                if re.search(rf"(?<!\w){re.escape(item.lower())}(?!\w)", haystack)
            )
            score += 0.42 * (matched_constraints / len(constraints))

        if query_terms:
            matched_terms = sum(1 for term in query_terms if term in haystack)
            score += 0.28 * (matched_terms / len(query_terms))

        has_query_number = bool(re.search(r"\b\d+[A-Za-z0-9/-]*\b", query))
        has_content_value = bool(re.search(r"\b(?:19|20)\d{2}(?:-\d{2})?\b|\b\d+(?:,\d{2,3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\s*(?:%|sq\.?m|sq\.?ft|lakh|crore|rs|inr)\b", haystack))
        if has_query_number and has_content_value:
            score += 0.15

        if any(term in haystack for term in ["official", "download", "pdf", "document", "table", "rate", "rates", "valuation", "survey", "section", "rule"]):
            score += 0.10

        return min(score, 1.0)

    def _extract_query_constraints(self, query: str) -> List[str]:
        normalized = self._normalize_digits(query or "")
        quoted = re.findall(r'"([^"]{2,80})"', normalized)
        years = re.findall(r"\b(?:19|20)\d{2}(?:-\d{2})?\b", normalized)
        labelled = re.findall(
            r"\b(?:no\.?|number|id|code|section|rule|article|survey|survay|srv|s\.?\s*no|gat|plot|cts|case|order|form|model|version)\s*(?:is|:|#|-)?\s*([A-Za-z0-9][A-Za-z0-9./_-]{0,40})",
            normalized,
            re.IGNORECASE,
        )
        compact_ids = re.findall(r"\b[A-Za-z]{1,8}[-/]?\d{1,8}(?:[-/][A-Za-z0-9]{1,12})*\b", normalized)
        constraints = quoted + years + labelled + compact_ids
        blocked = {"no", "number", "id", "code", "section", "rule", "article", "survey", "plot", "cts"}
        return list(dict.fromkeys(
            item.strip(" .,#:-").lower()
            for item in constraints
            if item.strip(" .,#:-") and item.strip(" .,#:-").lower() not in blocked
        ))

    def _important_query_terms(self, query: str) -> List[str]:
        stop_words = {
            "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for",
            "from", "give", "have", "how", "i", "in", "is", "it", "me", "most",
            "no", "number", "of", "on", "or", "regarding", "search", "show", "that", "the", "this",
            "to", "want", "what", "whatever", "when", "where", "which", "will",
            "with", "you", "please", "kindly",
        }
        return [
            word.lower()
            for word in re.findall(r"[A-Za-z0-9]+", self._normalize_digits(query or ""))
            if len(word) > 2 and word.lower() not in stop_words
        ][:16]

    def _has_strong_signal(self, url: str) -> bool:
        strong_terms = [
            "download", "pdf", "document", "report", "data", "rate", "rates",
            "valuation", "easr", "asr", "reckoner", "circle", "guideline",
            "notification", "manual", "annexure",
        ]
        url_lower = url.lower()
        return any(term in url_lower for term in strong_terms)

    def _is_pagination_link(self, url: str) -> bool:
        pagination_patterns = [
            r"[?&]page=\d+",
            r"[?&]p=\d+",
            r"[?&]offset=\d+",
            r"[?&]start=\d+",
            r"/page/\d+(?:/|$)",
        ]
        return any(re.search(pattern, url, re.IGNORECASE) for pattern in pagination_patterns)

    def _pagination_key(self, url: str) -> str:
        parsed = urlparse(url)
        path = re.sub(r"/page/\d+(?:/|$)", "/page/", parsed.path, flags=re.IGNORECASE)
        query = re.sub(r"([?&](?:page|p|offset|start)=)\d+", r"\1N", parsed.query, flags=re.IGNORECASE)
        return f"{parsed.netloc.lower()}{path}?{query}"

    def _should_follow_link(self, url: str, depth: int) -> bool:
        if depth == 0 or not self._is_pagination_link(url):
            return True

        key = self._pagination_key(url)
        count = self.pagination_seen.get(key, 0)
        if count >= 3:
            return False
        self.pagination_seen[key] = count + 1
        return True

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
            parsed = urlparse(url_lower)
            score = 0

            extension = PathLikeSuffix.from_url(parsed.path)
            if extension in query_terms:
                score += 30
            if "pdf" in query_terms and extension == "pdf":
                score += 30
            if any(year in url_lower for year in years):
                score += 20
            for term in query_terms:
                if term in url_lower:
                    score += 10
            if any(term in url_lower for term in [
                "official", "easr", "asr", "rate", "rates", "valuation", "reckoner",
                "circle", "guideline", "fsi", "far", "dcr", "dcpr", "udcpr",
                "development", "building", "zoning", "layout", "subdivision",
                "master-plan", "masterplan", "development-plan", "town-planning",
                "gazette", "notification",
            ]):
                score += 15

            scored_docs.append((score, -index, doc_url))

        scored_docs.sort(reverse=True)
        return [doc_url for _score, _index, doc_url in scored_docs]

    def _record_document_start(self, url: str) -> None:
        if not url or url in self.visited:
            return
        self.visited.add(url)
        self.results.append({
            "url": url,
            "depth": 0,
            "title": urlparse(url).path.rsplit("/", 1)[-1] or "Document",
            "content": "",
            "links": [],
            "document_links": [url],
        })

    def _get_cached_page(self, url: str) -> Optional[Dict]:
        if not self.crawl_cache:
            return None
        cached = self.crawl_cache.get(url, search_type="crawl")
        if not cached:
            return None
        print(f"Crawl cache hit for: {url}")
        return dict(cached)

    def _cache_page(self, url: str, result: Dict) -> None:
        if not self.crawl_cache:
            return
        self.crawl_cache.set(url, result, search_type="crawl")

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

    def _is_relevant_link(self, url: str, query_context: str, depth: int = 0) -> bool:
        if not self.llm_client or len(self.visited) < 5:
            return super()._is_relevant_link(url, query_context, depth)
        if depth > 0 and not self._has_strong_signal(url):
            return super()._is_relevant_link(url, query_context, depth)

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
            return super()._is_relevant_link(url, query_context, depth)


class PathLikeSuffix:
    """Tiny helper to keep document extension parsing isolated."""

    @staticmethod
    def from_url(path: str) -> str:
        match = re.search(r"\.([A-Za-z0-9]+)$", path or "")
        return match.group(1).lower() if match else ""
