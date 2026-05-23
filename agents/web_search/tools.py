from __future__ import annotations

"""
Response formatting - converts results to user-friendly format
Token usage: 0 (no LLM)
"""

from typing import List, Dict, Any
from datetime import datetime
import json
from dataclasses import asdict


class ResponseFormatter:
    """
    Format search results into various output formats
    Token usage: 0 (pure Python formatting)
    """

    def __init__(self):
        self.max_snippets = 10
        self.max_snippet_length = 300

    def format_markdown(self, query: str, results: List[Dict], analysis: str = None,
                        cached: bool = False, discovery: Dict = None) -> str:
        """Format results as markdown with clickable source URLs"""

        output = []
        output.append(f"# 🔍 Search Results: {query}")
        output.append(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        if cached:
            output.append("> 📦 **Results from cache**")

        output.append("")

        if discovery:
            output.append("## Step 1: Query Understanding & Source Discovery")
            output.append("")
            output.append(f"- Intent: {discovery.get('intent', 'research')}")
            output.append(f"- Key entities: {', '.join(discovery.get('key_entities', [])) or 'None'}")
            output.append(f"- Queries used: {' | '.join(discovery.get('rewritten_queries', []))}")
            output.append("")

        if analysis:
            output.append("## 📊 Analysis")
            output.append("")
            output.append(analysis)
            output.append("")

        output.append("## 📄 Search Results & Sources")
        output.append("")

        for i, result in enumerate(results[:10], 1):
            title = result.get('title', 'No title')
            snippet = result.get('snippet', 'No description')
            url = result.get('url', '#')
            date = result.get('published_date') or result.get('time_ago', 'Date unknown')

            if len(snippet) > self.max_snippet_length:
                snippet = snippet[:self.max_snippet_length] + "..."

            output.append(f"### {i}. {title}")
            output.append(f"**Date:** {date}")
            output.append("")
            output.append(f"{snippet}")
            output.append("")
            output.append(f"🔗 [Read more]({url})")
            output.append("")
            output.append("---")
            output.append("")

        # Add Sources Summary
        output.append("## 📌 Sources Summary")
        for result in results[:10]:
            title = result.get('title', 'Source')
            url = result.get('url', '#')
            output.append(f"- [{title}]({url})")

        reference_urls = self._collect_reference_urls(results)
        if reference_urls:
            output.append("")
            output.append("## Extraction Reference URLs")
            for i, url in enumerate(reference_urls[:20], 1):
                output.append(f"{i}. {url}")

        if len(results) > 10:
            output.append(f"\n*Plus {len(results) - 10} more results*")

        return "\n".join(output)

    def format_text(self, query: str, results: List[Dict], analysis: str = None,
                    discovery: Dict = None) -> str:
        """Format as plain text"""

        output = []
        output.append(f"SEARCH RESULTS: {query}")
        output.append("=" * 50)

        if analysis:
            output.append("")
            output.append("ANALYSIS:")
            output.append(analysis)
            output.append("")

        if discovery:
            output.append("STEP 1: QUERY UNDERSTANDING & SOURCE DISCOVERY")
            output.append(f"Intent: {discovery.get('intent', 'research')}")
            output.append(f"Key entities: {', '.join(discovery.get('key_entities', [])) or 'None'}")
            output.append(f"Queries used: {' | '.join(discovery.get('rewritten_queries', []))}")
            output.append("")

        output.append("SOURCES:")
        output.append("")

        for i, result in enumerate(results[:self.max_snippets], 1):
            output.append(f"{i}. {result.get('title', 'No title')}")
            output.append(f"   {result.get('snippet', 'No description')[:200]}")
            output.append(f"   URL: {result.get('url', '#')}")
            output.append("")

        reference_urls = self._collect_reference_urls(results)
        if reference_urls:
            output.append("EXTRACTION REFERENCE URLS:")
            for i, url in enumerate(reference_urls[:20], 1):
                output.append(f"{i}. {url}")
            output.append("")

        return "\n".join(output)

    def format_json(self, query: str, results: List[Dict], analysis: str = None,
                    token_usage: Dict = None, cached: bool = False,
                    discovery: Dict = None) -> str:
        """Format as JSON"""

        output = {
            'query': query,
            'timestamp': datetime.now().isoformat(),
            'cached': cached,
            'results_count': len(results),
            'results': [
                {
                    'title': r.get('title'),
                    'snippet': r.get('snippet'),
                    'url': r.get('url'),
                    'rank': i + 1,
                    'source': r.get('source'),
                    'search_query': r.get('search_query'),
                    'matched_entities': r.get('matched_entities', []),
                    'relevance_score': r.get('relevance_score'),
                    'llm_relevance_score': r.get('llm_relevance_score'),
                    'llm_relevance_reason': r.get('llm_relevance_reason'),
                    'trust_score': r.get('trust_score', r.get('source_trust')),
                    'verification_status': r.get('verification_status'),
                    'reference_urls': r.get('reference_urls', []),
                    'extraction_metadata': r.get('extraction_metadata')
                }
                for i, r in enumerate(results)
            ]
        }
        output['reference_urls'] = self._collect_reference_urls(results)
        output['source_traceability'] = self._build_source_traceability(results)

        if discovery:
            output['discovery'] = discovery

        if analysis:
            output['analysis'] = analysis

        if token_usage:
            output['token_usage'] = token_usage

        return json.dumps(output, indent=2, ensure_ascii=False)

    def _collect_reference_urls(self, results: List[Dict]) -> List[str]:
        urls = []
        for result in results or []:
            urls.extend(result.get('reference_urls') or [])
            if result.get('url'):
                urls.append(result['url'])
            document = result.get('document') or {}
            if document.get('source_url'):
                urls.append(document['source_url'])
            urls.extend(document.get('reference_urls') or [])
        return list(dict.fromkeys(url for url in urls if url))

    def _build_source_traceability(self, results: List[Dict]) -> Dict[str, List[Dict]]:
        traceability = {
            'top_verified_sources': [],
            'additional_sources': [],
            'crawled_source_urls': [],
            'document_source_urls': [],
            'extracted_evidence': [],
        }
        for index, result in enumerate(results or [], 1):
            url = result.get('url')
            if not url:
                continue
            source_type = result.get('source') or 'web'
            info = {
                'index': index,
                'title': result.get('title'),
                'url': url,
                'source': source_type,
                'trust_score': result.get('trust_score', result.get('source_trust')),
                'verification_status': result.get('verification_status', 'unverified'),
                'reference_urls': result.get('reference_urls', []),
            }
            if source_type == 'deep-crawl':
                traceability['crawled_source_urls'].append(info)
            elif source_type == 'document-extraction':
                info['document'] = result.get('document')
                traceability['document_source_urls'].append(info)
            elif info['verification_status'] == 'verified_indicator':
                traceability['top_verified_sources'].append(info)
            else:
                traceability['additional_sources'].append(info)

            for row in result.get('exact_ready_reckoner_rows', []) or []:
                if row.get('row_text'):
                    traceability['extracted_evidence'].append({
                        'source_index': index,
                        'source_url': url,
                        'source_title': result.get('title'),
                        'type': 'exact_ready_reckoner_row',
                        'text': row.get('row_text'),
                    })
            for match in result.get('exact_evidence_matches', []) or []:
                if match.get('text'):
                    traceability['extracted_evidence'].append({
                        'source_index': index,
                        'source_url': url,
                        'source_title': result.get('title'),
                        'type': 'exact_evidence_match',
                        'text': match.get('text'),
                    })
            traceability['extracted_evidence'].extend(
                self._structured_data_evidence(result.get('extracted_data'), index, url, result.get('title'))
            )

        traceability['top_verified_sources'].sort(key=lambda item: item.get('trust_score') or 0, reverse=True)
        traceability['additional_sources'].sort(key=lambda item: item.get('trust_score') or 0, reverse=True)
        return traceability

    def _structured_data_evidence(self, extracted_data, source_index: int, source_url: str, source_title: str = None) -> List[Dict]:
        if not extracted_data:
            return []
        if hasattr(extracted_data, '__dataclass_fields__'):
            extracted_data = asdict(extracted_data)
        if not isinstance(extracted_data, dict):
            return []

        evidence = []
        for fact in extracted_data.get('key_facts') or []:
            if fact:
                evidence.append({
                    'source_index': source_index,
                    'source_url': source_url,
                    'source_title': source_title,
                    'type': 'key_fact',
                    'text': str(fact),
                })

        for number in extracted_data.get('numbers') or []:
            if not isinstance(number, dict):
                continue
            value = number.get('value')
            context = number.get('context')
            text = f"{value}: {context}" if context else str(value or "")
            if text.strip():
                evidence.append({
                    'source_index': source_index,
                    'source_url': source_url,
                    'source_title': source_title,
                    'type': 'number',
                    'text': text,
                })

        for field_name in ('dates', 'locations', 'entities'):
            for value in extracted_data.get(field_name) or []:
                if value:
                    evidence.append({
                        'source_index': source_index,
                        'source_url': source_url,
                        'source_title': source_title,
                        'type': field_name[:-1],
                        'text': str(value),
                    })

        return evidence

    def format_streaming(self, query: str, result: Dict, is_last: bool = False) -> str:
        """Format for streaming response"""
        output = {
            'query': query,
            'title': result.get('title', ''),
            'snippet': result.get('snippet', ''),
            'url': result.get('url', ''),
            'is_last': is_last
        }
        return json.dumps(output) + "\n"

    def create_summary_table(self, results: List[Dict]) -> str:
        """Create a summary table for quick overview"""

        if not results:
            return "No results"

        table = "| # | Title | Source |\n"
        table += "|---|-------|--------|\n"

        for i, result in enumerate(results[:10], 1):
            title = result.get('title', '')[:50]
            domain = result.get('url', '').replace('https://', '').replace('http://', '').split('/')[0]
            table += f"| {i} | {title} | {domain} |\n"

        return table


# Example usage
if __name__ == "__main__":
    formatter = ResponseFormatter()

    sample_results = [
        {
            'title': '2BHK Flats in Baner',
            'snippet': 'Find 2BHK flats in Baner. Prices from ₹85 Lakhs.',
            'url': 'https://example.com/baner-2bhk'
        }
    ]

    markdown = formatter.format_markdown("2BHK Baner", sample_results)
    print(markdown)
