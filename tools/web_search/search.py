"""
LLM-based Web Search Implementation using OpenAI Responses API
Uses GPT-4o's built-in web search tool for accurate results
"""

import time
from typing import List, Optional
from dataclasses import dataclass
from core.web_search.config import config
import random
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse
from utils.web_search.timestamps import parse_date


@dataclass
class SearchResult:
    """Individual search result with optional quality metrics."""
    url: str
    title: str
    snippet: str
    source: str = "openai-websearch"
    rank: int = 0
    content: str = ""
    relevance_score: float = 0.0
    quality_score: float = 0.0
    content_type: str = "html"
    fetch_time: float = 0.0
    word_count: int = 0
    has_date: bool = False
    domain_authority: float = 0.0
    is_recent: bool = False


class DuckDuckGoSearcher:
    """
    DuckDuckGo search implementation using ddgs package
    (Fallback when LLM search is not available)
    """

    def __init__(self):
        self.request_delay = 1.0
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
        ]
        self.searxng_instances = [
            "https://searx.be",
            "https://searxng.world",
            "https://search.inetol.net",
            "https://searx.tiekoetter.com",
            "https://opnxng.com",
            "https://paulgo.io",
            "https://searx.bnyro.com",
        ]

    def _get_ddgs(self):
        try:
            from duckduckgo_search import DDGS
            return DDGS()
        except Exception:
            return None

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """Search the exact query across multiple no-key providers."""
        print(f"Searching exact web sources: '{query}'")

        all_results = []
        all_results.extend(self._search_duckduckgo_html(query, max_results))

        if len(self._dedupe_results(all_results)) < max_results:
            all_results.extend(self._search_bing(query, max_results))

        if len(self._dedupe_results(all_results)) < max_results:
            ddgs = self._get_ddgs()
            if ddgs:
                all_results.extend(self._search_ddgs_package(ddgs, query, max_results))
            else:
                print("   [!] DDGS package not available")

        if len(self._dedupe_results(all_results)) < max_results:
            all_results.extend(self._search_searxng(query, max_results))

        unique_results = self._dedupe_results(all_results)[:max_results]
        print(f"   Found {len(unique_results)} unique exact-query results")
        return unique_results

    def _headers(self, referer: str = "") -> dict:
        headers = {
            "User-Agent": random.choice(self.user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,en-IN;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _search_ddgs_package(self, ddgs, query: str, max_results: int) -> List[SearchResult]:
        search_results = []
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                results = list(ddgs.text(query, max_results=max_results, region='in-en'))
                for rank, result in enumerate(results, 1):
                    url = result.get('href', result.get('url', ''))
                    title = result.get('title', '')
                    snippet = result.get('body', result.get('description', ''))
                    if url and title:
                        search_results.append(SearchResult(
                            url=url,
                            title=title,
                            snippet=snippet,
                            source="duckduckgo",
                            rank=rank,
                        ))
                if search_results:
                    print(f"   DDGS package: {len(search_results)} results")
                    return search_results
                break
            except Exception as e:
                error_str = str(e).lower()
                is_rate_limit = "403" in error_str or "202" in error_str or "ratelimit" in error_str
                if is_rate_limit and attempt < max_retries and "exception occurred" not in error_str:
                    delay = 0.5 + (random.random() * 1.0)
                    print(f"   [-] DDGS rate limited, retrying in {delay:.1f}s...")
                    time.sleep(delay)
                    continue
                print(f"   [x] DDGS package failed: {error_str}")
                break
        return search_results

    def _search_duckduckgo_html(self, query: str, max_results: int = 10) -> List[SearchResult]:
        import requests
        from bs4 import BeautifulSoup
        import urllib.parse

        search_results = []
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            session = requests.Session()
            session.get("https://duckduckgo.com/", headers=self._headers(), timeout=6, verify=False)
            time.sleep(random.uniform(0.2, 0.5))

            params = urllib.parse.urlencode({"q": query, "kl": "us-en", "kp": "-1", "kaf": "1"})
            url = f"https://html.duckduckgo.com/html/?{params}"
            response = session.get(
                url,
                headers=self._headers("https://duckduckgo.com/"),
                timeout=10,
                verify=False,
            )
            soup = BeautifulSoup(response.text, "html.parser")

            for rank, block in enumerate(soup.select(".result"), 1):
                if len(search_results) >= max_results:
                    break
                link_tag = block.select_one(".result__a")
                if not link_tag:
                    continue
                title = link_tag.get_text(" ", strip=True)
                href = link_tag.get("href", "")
                parsed = urlparse(href)
                uddg = parse_qs(parsed.query).get("uddg", [""])[0]
                real_url = unquote(uddg) if uddg else href
                snippet_tag = block.select_one(".result__snippet")
                snippet = snippet_tag.get_text(" ", strip=True) if snippet_tag else ""
                if real_url.startswith("http") and title:
                    search_results.append(SearchResult(
                        url=real_url,
                        title=title,
                        snippet=re.sub(r"\s+", " ", snippet)[:250],
                        source="duckduckgo-html",
                        rank=rank,
                    ))
            if search_results:
                print(f"   DuckDuckGo HTML: {len(search_results)} results")
        except Exception as ex:
            print(f"   [x] DuckDuckGo HTML failed: {ex}")
        return search_results

    def _search_searxng(self, query: str, max_results: int = 10) -> List[SearchResult]:
        import requests
        import urllib.parse

        instances = self.searxng_instances[:]
        random.shuffle(instances)
        for instance in instances:
            search_results = []
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                params = urllib.parse.urlencode({
                    "q": query,
                    "format": "json",
                    "categories": "general",
                    "language": "en",
                    "safesearch": "0",
                })
                url = f"{instance}/search?{params}"
                response = requests.get(
                    url,
                    headers={**self._headers(instance), "Accept": "application/json, text/javascript, */*"},
                    timeout=8,
                    verify=False,
                )
                data = response.json()
                for rank, item in enumerate(data.get("results", [])[:max_results], 1):
                    url_val = item.get("url", "")
                    title = item.get("title", "")
                    snippet = item.get("content", "")
                    if url_val and title:
                        search_results.append(SearchResult(
                            url=url_val,
                            title=title,
                            snippet=re.sub(r"\s+", " ", snippet)[:250],
                            source="searxng",
                            rank=rank,
                        ))
                if search_results:
                    print(f"   SearXNG: {len(search_results)} results")
                    return search_results
            except Exception:
                continue
        print("   [!] SearXNG returned no results")
        return []

    def _dedupe_results(self, results: List[SearchResult]) -> List[SearchResult]:
        seen = set()
        unique = []
        for result in results:
            parsed = urlparse(result.url)
            key = f"{parsed.netloc.lower().replace('www.', '')}{parsed.path.rstrip('/')}"
            if not key or key in seen:
                continue
            seen.add(key)
            result.rank = len(unique) + 1
            unique.append(result)
        return unique

    def _search_bing(self, query: str, max_results: int = 5) -> List[SearchResult]:
        import requests
        from bs4 import BeautifulSoup
        import urllib.parse
        import base64
        import re

        search_results = []
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9,en-IN;q=0.8'
            }
            url = f'https://www.bing.com/search?q={urllib.parse.quote(query)}&setmkt=en-IN&setlang=en'
            # Disable SSL verification for local agent to avoid certificate errors on Windows
            resp = requests.get(url, headers=headers, timeout=10, verify=False)
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            soup = BeautifulSoup(resp.text, 'html.parser')

            for rank, li in enumerate(soup.find_all('li', class_='b_algo'), 1):
                title_tag = li.find('h2')
                if not title_tag: continue
                a_tag = title_tag.find('a')
                if not a_tag: continue
                link = a_tag.get('href', '')
                title = a_tag.text.strip()
                if not title:
                    title = a_tag.get('title', '').strip()
                if not title:
                    # Fallback to domain name
                    title = urlparse(link).netloc.replace('www.', '')

                # Try to decode Bing redirect URL
                if 'bing.com/ck' in link:
                    match = re.search(r'u=a1([^&]+)', link)
                    if match:
                        try:
                            # Add padding if needed
                            b64_str = match.group(1)
                            b64_str += '=' * (-len(b64_str) % 4)
                            decoded = base64.b64decode(b64_str).decode('utf-8', errors='ignore')
                            if decoded.startswith('http'):
                                link = decoded
                        except:
                            pass

                snippet_tag = li.find('div', class_='b_caption') or li.find('p')
                snippet = snippet_tag.text if snippet_tag else ''

                # Filter out Chinese websites
                bad_domains = ['.cn/', '.cn', 'baidu.com', 'weibo.com']
                if any(bad in link.lower() for bad in bad_domains):
                    continue

                if link and title:
                    search_results.append(SearchResult(
                        url=link,
                        title=title,
                        snippet=snippet,
                        source="bing",
                        rank=rank
                    ))
                    if len(search_results) >= max_results:
                        break
        except Exception as ex:
            print(f"   ✗ Bing fallback failed: {ex}")
        return search_results



class EnhancedSearcher:
    """
    Enhanced search with multi-query support and source quality scoring.

    This class keeps DuckDuckGo as the primary real-URL search provider, then adds
    source quality metrics so downstream analysis can prefer trustworthy sources.
    """

    def __init__(self):
        self.base_searcher = DuckDuckGoSearcher()

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """Standard search method for compatibility"""
        return self.search_with_quality(query, max_results=max_results)

    def search_with_quality(self, query: str, max_results: int = 10, days_back: Optional[int] = None) -> List[SearchResult]:
        """Search and rank by combined relevance and quality score."""
        print(f"🔍 Enhanced Search: '{query}'")
        results = self._search_duckduckgo(query, max_results * 2)

        # Filter by recent results if requested
        if days_back:
            results = self.filter_by_recent_results(results, days_back=days_back)

        is_news = days_back is not None
        scored_results = []
        for result in results:
            result.content_type = self._detect_content_type(result.url)
            result.has_date = self._has_date(result)
            result.is_recent = self._is_recent(result)
            result.word_count = len(result.snippet.split())
            result.domain_authority = self._domain_authority(result.url)
            result.quality_score = self._calculate_quality_score(result)
            result.relevance_score = self._calculate_relevance_score(result, query)

            final_score = self.calculate_final_score(result, is_news=is_news)
            scored_results.append((final_score, result))

        scored_results.sort(key=lambda item: item[0], reverse=True)
        # Adaptive threshold: prefer high quality, but don't return zero if results exist
        threshold = 20 if days_back else 25
        final_results = [result for score, result in scored_results if score >= threshold]

        # If still no results, take the top 3 regardless of score
        if not final_results and scored_results:
            final_results = [result for score, result in scored_results[:3]]

        print(f"   ✓ Found {len(final_results[:max_results])} high-quality results")
        return final_results[:max_results]

    def filter_by_recent_results(self, results: List[SearchResult], days_back: int = 7) -> List[SearchResult]:
        """Filter search results to last N days only"""
        cutoff_date = datetime.now() - timedelta(days=days_back)
        filtered = []

        for result in results:
            # Try to extract date from snippet or metadata
            date_str = getattr(result, 'date_published', None)
            if not date_str:
                # Fallback: try to find date in snippet
                import re
                date_match = re.search(r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})', result.snippet)
                if date_match:
                    date_str = date_match.group(1)

            pub_date = parse_date(date_str) if date_str else None
            if pub_date:
                # Make timezone naive for comparison if needed
                if pub_date.tzinfo:
                    from datetime import timezone
                    pub_date = pub_date.astimezone(timezone.utc).replace(tzinfo=None)

                if pub_date >= cutoff_date:
                    filtered.append(result)
            elif "latest" in result.snippet.lower() or "today" in result.snippet.lower():
                # Keep results that claim to be latest/today even if date parsing fails
                filtered.append(result)

        return filtered

    def sort_by_date_desc(self, results: List[SearchResult]) -> List[SearchResult]:
        """Sort by date (newest first)"""
        return sorted(results, key=lambda x: parse_date(getattr(x, 'date_published', '')) or datetime.min, reverse=True)

    def score_result_recency(self, result: SearchResult) -> float:
        """Score results based on how recent they are"""
        date_str = getattr(result, 'date_published', None)
        pub_date = parse_date(date_str) if date_str else None

        if not pub_date:
            # If no date found, give a baseline score for news queries
            score = 30 if "latest" in result.snippet.lower() or "news" in result.snippet.lower() else 10
            return score

        # Make timezone naive
        if pub_date.tzinfo:
            from datetime import timezone
            pub_date = pub_date.astimezone(timezone.utc).replace(tzinfo=None)

        days_old = (datetime.now() - pub_date).days

        if days_old <= 1:
            return 100  # Today/yesterday
        elif days_old <= 3:
            return 80   # Within 3 days
        elif days_old <= 7:
            return 50   # Within a week
        elif days_old <= 14:
            return 20   # Within 2 weeks
        else:
            return 0    # Older - exclude or downrank

    def calculate_final_score(self, result: SearchResult, is_news: bool = False) -> float:
        """Combine recency and relevance scores"""
        relevance_score = getattr(result, 'relevance_score', 0)

        if is_news:
            recency_score = self.score_result_recency(result)
            # Weight recency more heavily (70/30 split as requested)
            return (recency_score * 0.7) + (relevance_score * 0.3)
        else:
            # For research, relevance is king, but boost if fresh
            recency_score = self.score_result_recency(result)
            recency_bonus = (recency_score / 100.0) * 10 # Max 10 point bonus for fresh research
            return relevance_score + recency_bonus

    def search_parallel(self, queries: List[str], max_results: int = 5, days_back: int = 7) -> List[SearchResult]:
        """Search multiple query variants and deduplicate by URL."""
        all_results = []
        for query in queries:
            all_results.extend(self.search_with_quality(query, max_results, days_back=days_back))

        seen_urls = set()
        unique_results = []
        for result in all_results:
            if result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            unique_results.append(result)

        unique_results.sort(
            key=lambda item: (item.relevance_score * 0.7) + (item.quality_score * 0.3),
            reverse=True,
        )
        return unique_results[:max_results]

    def _search_duckduckgo(self, query: str, max_results: int) -> List[SearchResult]:
        return self.base_searcher.search(query, max_results=max_results)

    def _calculate_quality_score(self, result: SearchResult) -> float:
        score = 50.0
        score = max(score, result.domain_authority)

        title = result.title.lower()
        snippet = result.snippet.lower()

        if len(result.title) > 30:
            score += 5
        if any(word in title for word in ["official", "government", "rera", "guideline", "notification"]):
            score += 10
        if result.is_recent:
            score += 8
        if len(result.snippet) > 100:
            score += 5
        if any(word in snippet for word in ["price", "rate", "unit", "sqft", "sq.ft", "bhk", "rule", "act"]):
            score += 10
        if result.content_type == "pdf":
            score += 5

        if any(word in snippet for word in ["login", "sign up", "subscribe", "newsletter"]):
            score -= 10

        return min(max(score, 0), 100)

    def _calculate_relevance_score(self, result: SearchResult, query: str) -> float:
        query_words = [
            word.lower()
            for word in query.split()
            if len(word) > 2 and word.lower() not in {"what", "how", "the", "and", "for", "with"}
        ]
        text = f"{result.title.lower()} {result.snippet.lower()} {result.url.lower()}"

        if not query_words:
            return 0.0

        matched_words = sum(1 for word in query_words if word in text)
        keyword_score = (matched_words / len(query_words)) * 100

        if query.lower() in text:
            keyword_score += 20

        return min(keyword_score, 100)

    def _domain_authority(self, url: str) -> float:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        path = urlparse(url).path.lower()
        score = 50.0

        if domain.endswith(".gov.in") or domain.endswith(".nic.in"):
            score += 35.0
        elif ".gov." in domain or domain.endswith(".gov"):
            score += 30.0
        elif domain.endswith(".edu") or ".edu." in domain or domain.endswith(".ac.in"):
            score += 20.0

        if path.endswith(".pdf"):
            score += 5.0
        if len(domain.split(".")) <= 3:
            score += 5.0
        if any(part in domain for part in ["login", "account", "ads", "tracking"]):
            score -= 15.0

        return min(max(score, 0.0), 100.0)

    def _detect_content_type(self, url: str) -> str:
        path = urlparse(url).path.lower()
        if path.endswith(".pdf"):
            return "pdf"
        if path.endswith(".json"):
            return "json"
        return "html"

    def _has_date(self, result: SearchResult) -> bool:
        text = f"{result.title} {result.snippet}"
        return any(str(year) in text for year in range(datetime.now().year - 3, datetime.now().year + 1))

    def _is_recent(self, result: SearchResult) -> bool:
        current_year = datetime.now().year
        text = f"{result.title} {result.snippet}".lower()
        return str(current_year) in text or str(current_year - 1) in text or "latest" in text
