"""
DuckDuckGo Web Search Agent - Main Entry Point
Complete working agent with minimal token usage
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import asyncio
import re
import sys
from dataclasses import asdict

from tools.web_search.search import DuckDuckGoSearcher
from tools.web_search.discovery import SourceDiscovery
from tools.web_search.browser import ContentProcessor
from tools.web_search.crawler import LLMGuidedCrawler, WebCrawler
from tools.web_search.document_downloader import DocumentDownloader
from tools.web_search.weather import WeatherLookup
from agents.web_search.prompts import LightweightAnalyzer
from database.web_search.cache import SearchCache
from core.web_search.config import config
from utils.web_search.validation import AccuracyValidator

SEARCH_CACHE_VERSION = "source-discovery-v14-llm-source-rerank"


class DuckDuckGoSearchAgent:
    """
    Complete search agent using free DuckDuckGo API
    Token usage: Only for LLM analysis (500-2000 tokens per complex query)
    """

    def __init__(self):
        # Primary searcher is DuckDuckGo (with Bing fallback)
        self.searcher = DuckDuckGoSearcher()
        self.discovery = SourceDiscovery(self.searcher)
        self.analyzer = LightweightAnalyzer()
        self.processor = ContentProcessor()
        if config.USE_LLM_GUIDED_CRAWL and self.analyzer.client:
            self.crawler = LLMGuidedCrawler(self.analyzer.client)
        else:
            self.crawler = WebCrawler()
        self.downloader = DocumentDownloader()
        self.weather = WeatherLookup()
        self.cache = SearchCache() if config.CACHE_ENABLED else None
        self.validator = AccuracyValidator()

        # Stats
        self.stats = {
            'queries': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'total_tokens': 0,
            'total_cost': 0.0
        }

    def search(self, query: str, max_results: int = 10,
               fetch_content: bool = True, use_cache: bool = True, status_callback=None,
               stream_callback=None, debug_llm_payloads: bool = False) -> Dict:
        """
        Perform search and return results
        """

        cache_query = f"{SEARCH_CACHE_VERSION}:{query}"

        # Check cache
        if use_cache and self.cache:
            cached = self.cache.get(cache_query)
            if cached:
                self.stats['cache_hits'] += 1
                cached['cached'] = True
                return cached

        self.stats['cache_misses'] += 1
        self.stats['queries'] += 1

        # Weather questions need live structured data, not generic search snippets.
        if self.weather.is_weather_query(query):
            if status_callback:
                status_callback('Fetching current weather data...')
            weather_output = self._search_weather(query, stream_callback=stream_callback)
            if use_cache and self.cache and weather_output.get("success"):
                self.cache.set(cache_query, weather_output)
            return weather_output

        # Step 1: Query understanding and source discovery
        if status_callback: status_callback('Understanding query and discovering sources...')
        discovery = self.discovery.discover(query, max_results, debug_llm_payloads=debug_llm_payloads, status_callback=status_callback)
        search_results = discovery["results"]

        if not search_results:
            return {
                'query': query,
                'success': False,
                'error': 'No results found',
                'results': []
            }

        # Convert to dict format
        results_dict = [
            {
                'url': r['url'],
                'title': r['title'],
                'snippet': r['snippet'],
                'rank': r['rank'],
                'source': r.get('source'),
                'search_query': r.get('search_query'),
                'matched_entities': r.get('matched_entities', []),
                'relevance_score': r.get('relevance_score', 0.0)
            }
            for r in search_results
        ]

        # Fetch full content if requested
        if fetch_content:
            urls = [r['url'] for r in search_results[:min(max_results, len(search_results))]]
            if status_callback: status_callback(f'Reading full content from {len(urls)} top sources...')
            content_results = self.processor.process_batch(urls, query=query, status_callback=status_callback)

            # Filter and Merge content
            intent = discovery.get("understanding", {}).get("intent")
            if content_results:
                final_results = []
                for result in results_dict:
                    found_content = False
                    for content in content_results:
                        if content and result['url'] == content.get('url'):
                            # Apply real estate filtering for construction queries
                            content_text = content.get('content', '')
                            if intent == "construction_status":
                                if self.processor._is_tourism_content(content_text):
                                    if status_callback: status_callback(f"Skipping tourism content: {result['url'][:30]}...")
                                    continue

                            result['content'] = content_text
                            result['title'] = content.get('title') or result['title']
                            result['published_date'] = content.get('published_date')
                            result['time_ago'] = content.get('time_ago', 'Date unknown')
                            result['source_trust'] = content.get('source_trust', 0.5)
                            result['exact_ready_reckoner_rows'] = content.get('exact_ready_reckoner_rows', [])
                            result['exact_evidence_matches'] = content.get('exact_evidence_matches', [])
                            result['extracted_data'] = content.get('extracted_data')
                            found_content = True
                            break
                    final_results.append(result)
                results_dict = final_results

        if config.ENABLE_CRAWLING and search_results:
            crawled_sources, document_sources = self._crawl_and_extract_documents(
                query,
                results_dict,
                status_callback=status_callback,
            )
            if crawled_sources or document_sources:
                known_urls = {result.get('url') for result in results_dict}
                for extra in crawled_sources + document_sources:
                    if extra.get('url') and extra.get('url') not in known_urls:
                        known_urls.add(extra.get('url'))
                        results_dict.append(extra)

        # Analyze if needed
        analysis = None
        token_before = self.analyzer.get_token_report()
        if debug_llm_payloads:
            self.analyzer.last_llm_payloads = []

        if self.analyzer.needs_analysis(query, results_dict):
            if status_callback: status_callback('Analyzing data and generating trusted answer...')
            intent = discovery.get("understanding", {}).get("intent")
            trusted_response = self.analyzer.generate_trusted_answer(
                query,
                results_dict,
                self.validator,
                intent=intent,
                stream_callback=stream_callback,
                debug_llm_payloads=debug_llm_payloads
            )
            analysis = trusted_response['answer']
            output_metadata = {
                'accuracy_score': trusted_response['accuracy_score'],
                'confidence_level': trusted_response['confidence_level'],
                'recommendation': trusted_response['recommendation'],
                'validated_claims': trusted_response['validated_claims']
            }
        else:
            analysis = self.analyzer.build_source_based_answer(query, results_dict)
            if stream_callback and analysis:
                stream_callback(analysis)
            output_metadata = {
                'accuracy_score': 0,
                'confidence_level': 'Source-based fallback',
                'recommendation': 'Verify details on the linked source pages',
                'validated_claims': []
            }

        if not str(analysis or "").strip():
            analysis = self.analyzer.build_source_based_answer(query, results_dict)
            if stream_callback and analysis:
                stream_callback(analysis)

        # Prepare output
        output = {
            'query': query,
            'success': True,
            'discovery': discovery["understanding"],
            'discovery_token_usage': discovery.get("token_usage"),
            'results_count': len(results_dict),
            'results': results_dict,
            'analysis': analysis,
            'accuracy': output_metadata,
            'timestamp': datetime.now().isoformat()
        }

        if debug_llm_payloads:
            output['llm_debug_payloads'] = (
                discovery.get("llm_debug_payloads", []) + self.analyzer.last_llm_payloads
            )

        # Convert any dataclasses to dicts for JSON serialization
        for result in results_dict:
            extracted = result.get('extracted_data')
            if extracted and hasattr(extracted, '__dataclass_fields__'):
                result['extracted_data'] = asdict(extracted)

        # Also handle discovery understanding if it's a dataclass
        if hasattr(output['discovery'], '__dataclass_fields__'):
            output['discovery'] = asdict(output['discovery'])

        # Handle validated_claims in accuracy metadata
        if 'accuracy' in output and 'validated_claims' in output['accuracy']:
            claims = output['accuracy']['validated_claims']
            if isinstance(claims, list):
                output['accuracy']['validated_claims'] = [
                    asdict(c) if hasattr(c, '__dataclass_fields__') else c for c in claims
                ]

        # Add token usage
        token_after = self.analyzer.get_token_report()
        token_report = {
            'input_tokens': token_after['input_tokens'] - token_before['input_tokens'],
            'output_tokens': token_after['output_tokens'] - token_before['output_tokens'],
            'total_cost': round(token_after['total_cost'] - token_before['total_cost'], 6),
        }
        token_report['total_tokens'] = token_report['input_tokens'] + token_report['output_tokens']
        output['token_usage'] = token_report

        discovery_tokens = discovery.get("token_usage") or {}
        self.stats['total_tokens'] += token_report['total_tokens'] + discovery_tokens.get('total_tokens', 0)
        self.stats['total_cost'] += token_report['total_cost'] + discovery_tokens.get('total_cost', 0.0)

        # Cache results
        if use_cache and self.cache:
            self.cache.set(cache_query, output)

        return output

    def _search_weather(self, query: str, stream_callback=None) -> Dict:
        weather_result = self.weather.lookup(query)
        if not weather_result.get("success"):
            message = (
                f"I could not fetch current weather data for this query. "
                f"{weather_result.get('error') or 'Please try a more specific city name.'}"
            )
            if stream_callback:
                stream_callback(message)
            return {
                'query': query,
                'success': False,
                'error': weather_result.get('error'),
                'results': [],
                'analysis': message,
                'accuracy': {
                    'accuracy_score': 0,
                    'confidence_level': 'Low',
                    'recommendation': 'Try a more specific city and country',
                    'validated_claims': []
                },
                'timestamp': datetime.now().isoformat()
            }

        analysis = weather_result['analysis']
        if stream_callback:
            stream_callback(analysis)

        return {
            'query': query,
            'success': True,
            'discovery': {
                'original_query': query,
                'intent': 'weather',
                'key_entities': [weather_result.get('location_query')],
                'rewritten_queries': [],
                'positive_terms': ['weather', 'temperature'],
                'avoid_terms': [],
                'used_llm': False,
                'is_real_estate': False,
            },
            'discovery_token_usage': {
                'input_tokens': 0,
                'output_tokens': 0,
                'total_tokens': 0,
                'total_cost': 0.0,
            },
            'results_count': len(weather_result['results']),
            'results': weather_result['results'],
            'analysis': analysis,
            'accuracy': {
                'accuracy_score': 100,
                'confidence_level': 'High - live weather API',
                'recommendation': 'Weather values are time-sensitive; refresh for the latest reading.',
                'validated_claims': []
            },
            'timestamp': weather_result.get('timestamp') or datetime.now().isoformat(),
            'token_usage': {
                'input_tokens': 0,
                'output_tokens': 0,
                'total_tokens': 0,
                'total_cost': 0.0,
            },
            'weather': {
                'location': weather_result.get('place'),
                'current': weather_result.get('weather'),
            },
        }

    def _crawl_and_extract_documents(self, query: str, results: List[Dict], status_callback=None) -> tuple[List[Dict], List[Dict]]:
        if not results:
            return [], []

        urls = [
            result.get('url')
            for result in results[: max(1, config.CRAWL_TOP_RESULTS)]
            if result.get('url')
        ]
        if not urls:
            return [], []

        try:
            if status_callback:
                status_callback(f"Crawling {len(urls)} top sources for deeper evidence...")
            crawled_pages = self._run_async(self.crawler.crawl_many(urls, query))
        except Exception as exc:
            print(f"Deep crawling failed: {exc}")
            crawled_pages = []

        crawled_sources = self._crawled_pages_to_results(query, crawled_pages)
        document_sources = []

        if config.ENABLE_DOCUMENT_DOWNLOAD and crawled_pages:
            try:
                if status_callback:
                    status_callback("Downloading and reading linked documents...")
                documents = self._run_async(self.downloader.find_and_download_documents(crawled_pages, query_context=query))
                document_sources = self._documents_to_results(query, documents)
            except Exception as exc:
                print(f"Document extraction failed: {exc}")

        return crawled_sources, document_sources

    def _run_async(self, coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result_box = {}
        error_box = {}

        def runner():
            try:
                result_box["result"] = asyncio.run(coro)
            except Exception as exc:
                error_box["error"] = exc

        import threading

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()
        if error_box:
            raise error_box["error"]
        return result_box.get("result")

    def _crawled_pages_to_results(self, query: str, crawled_pages: List[Dict]) -> List[Dict]:
        converted = []
        for index, page in enumerate(crawled_pages or [], 1):
            content = page.get('content') or ''
            if not content:
                continue
            exact_rows = self.processor._extract_exact_ready_reckoner_rows(content, query)
            exact_matches = self.processor._extract_exact_evidence_matches(content, query)
            converted.append({
                'url': page.get('url'),
                'title': page.get('title') or f"Crawled page {index}",
                'snippet': content[:400],
                'content': content,
                'rank': 100 + index,
                'source': 'deep-crawl',
                'search_query': query,
                'matched_entities': [],
                'relevance_score': self._content_relevance(query, content),
                'source_trust': 0.45,
                'exact_ready_reckoner_rows': exact_rows,
                'exact_evidence_matches': exact_matches,
            })
        return converted

    def _documents_to_results(self, query: str, documents: List[Dict]) -> List[Dict]:
        converted = []
        for index, doc in enumerate(documents or [], 1):
            content = doc.get('content') or ''
            if not content:
                continue
            exact_rows = self.processor._extract_exact_ready_reckoner_rows(content, query)
            exact_matches = self.processor._extract_exact_evidence_matches(content, query)
            converted.append({
                'url': doc.get('url'),
                'title': doc.get('filename') or f"Document {index}",
                'snippet': content[:400],
                'content': content,
                'rank': 200 + index,
                'source': 'document-extraction',
                'search_query': query,
                'matched_entities': [],
                'relevance_score': self._content_relevance(query, content) + 0.1,
                'source_trust': 0.55,
                'published_date': None,
                'time_ago': 'Date unknown',
                'exact_ready_reckoner_rows': exact_rows,
                'exact_evidence_matches': exact_matches,
                'document': {
                    'filename': doc.get('filename'),
                    'filepath': doc.get('filepath'),
                    'source_url': doc.get('source_url'),
                    'size': doc.get('size'),
                    'content_type': doc.get('content_type'),
                },
            })
        return converted

    def _content_relevance(self, query: str, content: str) -> float:
        terms = [
            word.lower()
            for word in re.findall(r"[A-Za-z0-9]+", query)
            if len(word) > 2
        ]
        if not terms:
            return 0.2
        content_lower = content.lower()
        matched = sum(1 for term in terms if term in content_lower)
        return min(max(matched / len(terms), 0.1), 1.0)

    def extract_from_url(self, url: str, query: str) -> Dict:
        """Extract exact data from a given URL"""
        self.stats['queries'] += 1
        content_results = self.processor.process_batch([url])

        if not content_results or not content_results[0]:
            return {'url': url, 'success': False, 'error': 'Could not fetch content'}

        content = content_results[0]
        return {
            'url': url,
            'success': True,
            'title': content.get('title'),
            'content': content.get('content'),
            'metadata': content.get('metadata')
        }
