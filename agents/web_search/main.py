"""
DuckDuckGo Web Search Agent - Main Entry Point
Complete working agent with minimal token usage
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import sys
from dataclasses import asdict

from tools.web_search.search import DuckDuckGoSearcher
from tools.web_search.discovery import SourceDiscovery
from tools.web_search.browser import ContentProcessor
from agents.web_search.prompts import LightweightAnalyzer
from database.web_search.cache import SearchCache
from core.web_search.config import config
from utils.web_search.validation import AccuracyValidator

SEARCH_CACHE_VERSION = "source-discovery-v8-specific-fallback"


class DuckDuckGoSearchAgent:
    """
    Complete search agent using free DuckDuckGo API
    Token usage: Only for LLM analysis (500-2000 tokens per complex query)
    """

    def __init__(self):
        # Primary searcher is DuckDuckGo (with Bing fallback)
        self.searcher = DuckDuckGoSearcher()
        self.discovery = SourceDiscovery(self.searcher)
        self.processor = ContentProcessor()
        self.analyzer = LightweightAnalyzer()
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
            urls = [r['url'] for r in search_results[:min(5, len(search_results))]]
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
