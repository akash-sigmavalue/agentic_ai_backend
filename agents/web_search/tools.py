"""
Response formatting - converts results to user-friendly format
Token usage: 0 (no LLM)
"""

from typing import List, Dict, Any
from datetime import datetime
import json


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
                    'llm_relevance_reason': r.get('llm_relevance_reason')
                }
                for i, r in enumerate(results)
            ]
        }

        if discovery:
            output['discovery'] = discovery

        if analysis:
            output['analysis'] = analysis

        if token_usage:
            output['token_usage'] = token_usage

        return json.dumps(output, indent=2, ensure_ascii=False)

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
