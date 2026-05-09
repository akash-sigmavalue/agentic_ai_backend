"""
LLM-based analysis - MINIMAL TOKEN USAGE
Only called when needed, uses GPT-4o-mini for cost efficiency
Token usage: 500-2000 per analysis
"""

import json
import re
from typing import List, Dict, Any, Optional
from datetime import datetime
from openai import OpenAI
import tiktoken
from core.web_search.config import config


class LightweightAnalyzer:
    """
    Lightweight analyzer using LLM for accurate answers to all query types
    Token usage: 500-2000 tokens per query
    """

    def __init__(self):
        self.client = None
        self.encoder = None

        if config.USE_LLM and config.OPENAI_API_KEY:
            self.client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=45)
            self.encoder = tiktoken.encoding_for_model("gpt-4o-mini")

        self.token_usage = {
            'input_tokens': 0,
            'output_tokens': 0,
            'total_cost': 0.0,
            'query_count': 0
        }
        self.last_llm_payloads = []

    def needs_analysis(self, query: str, results: List) -> bool:
        """Determine if LLM analysis is needed"""
        # Always use LLM for accurate answers when available
        if self.client:
            return True

        # Use LLM only for complex queries
        complex_indicators = [
            'compare', 'analysis', 'trend', 'vs', 'versus',
            'highest', 'lowest', 'best', 'worst',
            'why', 'how', 'what is the difference'
        ]

        query_lower = query.lower()

        # Simple keyword extraction doesn't need LLM
        if len(results) <= 3 and not any(ind in query_lower for ind in complex_indicators):
            return False

        return True


    def get_token_report(self) -> Dict:
        """Get token usage report"""
        return {
            'input_tokens': self.token_usage['input_tokens'],
            'output_tokens': self.token_usage['output_tokens'],
            'total_cost': round(self.token_usage['total_cost'], 6),
            'cost_per_query': round(self.token_usage['total_cost'] / max(1, self.token_usage.get('query_count', 1)), 6)
        }

    def generate_trusted_answer(
        self,
        query: str,
        results: List[Dict],
        validator,
        intent: str = None,
        stream_callback=None,
        debug_llm_payloads: bool = False,
    ) -> Dict:
        """Generate a trusted answer with validation stats"""
        try:
            self.last_llm_payloads = []
            if not results:
                is_news = any(kw in query.lower() for kw in ['news', 'latest', 'recent', 'today'])
                if is_news:
                    msg = f"I'm sorry, but I couldn't find any recent news updates for '{query}' from the last 7 days. This might be due to search provider rate limits or a lack of new articles matching your criteria. Please try again with a broader query."
                else:
                    msg = "I'm sorry, but I couldn't find any specific results for your query at the moment. This might be because search providers are currently rate-limiting my requests. Please try again in a few minutes."

                if stream_callback: stream_callback(msg)
                return {
                    'answer': msg,
                    'accuracy_score': 0,
                    'validated_claims': [],
                    'sources_agreed': 0,
                    'recommendation': "Try broader query",
                    'confidence_level': "Low"
                }

            # First validate findings
            extracted_objects = [r.get('extracted_data') for r in results if r.get('extracted_data')]
            validation = validator.cross_validate(extracted_objects, query)

            # Prepare context with validation info
            prompt = self._build_accuracy_prompt(query, results, validation, intent)

            # Get LLM answer
            if stream_callback:
                answer = self._get_llm_answer_with_confidence_stream(
                    prompt,
                    stream_callback,
                    debug_llm_payloads=debug_llm_payloads,
                )
            else:
                answer = self._get_llm_answer_with_confidence(prompt, debug_llm_payloads=debug_llm_payloads)

            # Post-process for real estate queries
            if intent == "construction_status" or "project" in query.lower():
                answer = self.validate_real_estate_content(answer, query)

            return {
                'answer': answer or "Failed to generate answer.",
                'accuracy_score': validation.get('accuracy_score', 0),
                'validated_claims': validation.get('validated_claims', []),
                'sources_agreed': validation.get('sources_agreed', 0),
                'recommendation': validation.get('recommendation', "Verify independently"),
                'confidence_level': self._get_confidence_level(validation.get('accuracy_score', 0))
            }
        except Exception as e:
            print(f"Error in generate_trusted_answer: {e}")
            return {
                'answer': f"An error occurred during analysis: {str(e)}",
                'accuracy_score': 0,
                'validated_claims': [],
                'sources_agreed': 0,
                'recommendation': "Error during validation",
                'confidence_level': "🔴 Error"
            }

    def _build_accuracy_prompt(self, query: str, results: List[Dict], validation: Dict, intent: str = None) -> str:
        source_context = []
        for i, r in enumerate(results[:10], 1):
            content = r.get('content') or r.get('snippet') or "No content available."
            source_context.append(f"[{i}] {r.get('title')}\nURL: {r.get('url')}\nTrust Score: {r.get('source_trust', 0.5)*100:.0f}%\nContent: {content[:5000]}")

        validated_str = "\n".join([f"- {c['claim']} (Verified by {c['source_count']} sources)" for c in validation.get('validated_claims', [])])
        sources_str = "\n\n".join(source_context)

        if intent == "construction_status" or "project" in query.lower():
            import re
            year_match = re.search(r'20\d{2}', query)
            year = year_match.group(0) if year_match else '2026'
            location = "Pune"
            locations = ['Pune', 'Mumbai', 'Bangalore', 'Wakad', 'Baner', 'Hinjewadi', 'Kharadi']
            for loc in locations:
                if loc.lower() in query.lower():
                    location = loc
                    break

            return f"""You are a real estate market analyst.

## CRITICAL RULES - STRICTLY FOLLOW:

1. **ONLY** provide information about REAL ESTATE PROJECTS (residential/commercial flats, apartments, villas, plots)
2. **NEVER** mention tourist attractions, restaurants, hotels, or places to visit
3. **NEVER** use Tripadvisor, travel sites, or tourism sources
4. **ALWAYS** prioritize official registration, developer disclosures, and sources with direct evidence
5. **ALWAYS** include for each project: name, builder, location, status, possession date

## User Query: {query}

## Available Sources (Real Estate Only):
{sources_str}

## FORMAT YOUR ANSWER AS:

### New Residential Projects in {location} ({year})

| Project Name | Builder | Location | Status | Possession | Units | Price Range |
|--------------|---------|----------|--------|------------|-------|-------------|
| [Name] | [Builder] | [Area] | New Launch/UC | [Date] | [Number] | [₹ Range] |

### Key Highlights
- [Important point 1]
- [Important point 2]

### RERA Status
- [Registration details if available]

### Builder Information
- [About the developer]

**If no project information found in the sources, say: "No new project announcements found for {location} in {year}. Check RERA website or real estate portals for official updates."**
"""

        if intent == "construction_status":
            return f"""You are a real estate expert. Answer this query about under-construction projects.

CRITICAL INSTRUCTIONS:
- ONLY list actual residential/commercial projects with their status
- DO NOT list tourist attractions, restaurants, or entertainment venues
- IGNORE any "things to do", "shopping", "dining" content
- Use only sources whose retrieved content is actually about real estate projects
- Prefer official registration records, developer pages, filings, PDFs, or data-rich listings when present
- For each project, include: name, builder, possession date, total units, current status

CROSS-SOURCE VALIDATION DATA:
{validated_str or "No cross-source consensus found."}

SEARCH RESULTS CONTEXT:
{"-"*20}
{sources_str}
{"-"*20}

Format your answer as:
1. **Project Name** by Builder Name
   - Status: Under construction / New launch
   - Expected possession: Q3 2026
   - Total units: XXX
   - Price range: ₹XX - ₹XX Lakhs

If no under-construction projects found in sources, say so clearly.

Answer:"""

        return f"""You are a high-accuracy fact-checking search assistant.
User Query: "{query}"

CROSS-SOURCE VALIDATION DATA:
{validated_str or "No cross-source consensus found for specific numbers/facts."}

SEARCH RESULTS CONTEXT:
{"-"*20}
{sources_str}
{"-"*20}

INSTRUCTIONS:
1. Provide an EXTREMELY DETAILED and COMPREHENSIVE answer (aim for 90% extraction of all relevant facts).
2. Structure your response with high-density information:
   - ## 📝 Executive Summary
   - ## 🔍 Exhaustive Findings (Extract EVERY specific number, fee, date, and legal rule found)
   - ## ⚖️ Legal/Regulatory Context (Specific acts, sections, and official departments)
   - ## ⚠️ Penalties & Enforcement (If applicable, extract specific amounts and durations)
3. Use internal citations [1], [2], etc., for EVERY factual claim.
4. DO NOT SUMMARIZE. If a source provides a list of 5 changes, list all 5 with their exact values.
5. If sources conflict, explicitly mention the contradiction.
6. Use tables to present numerical data or comparisons.
7. End with a "Confidence Insight" section.
8. CRITICAL: At the very end, add a section "### Reference URLs" with clickable markdown links [Title](URL) for the first 10 provided sources. Do not stop at 5 when 10 sources are available.

Answer:"""

    def _get_llm_answer_with_confidence(self, prompt: str, debug_llm_payloads: bool = False) -> str:
        try:
            payload = {
                "model": config.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": config.MAX_TOKENS * 2,
                "temperature": 0.2,
            }
            if debug_llm_payloads:
                self.last_llm_payloads.append({
                    "stage": "final_answer_generation",
                    "target_file": "backend/agents/UI_dashboard/prompts.py",
                    "target_function": "LightweightAnalyzer._get_llm_answer_with_confidence",
                    "payload": payload,
                })
            response = self.client.chat.completions.create(
                **payload
            )
            self.token_usage['input_tokens'] += response.usage.prompt_tokens
            self.token_usage['output_tokens'] += response.usage.completion_tokens
            self.token_usage['total_cost'] += (response.usage.prompt_tokens * 0.00000015) + (response.usage.completion_tokens * 0.0000006)
            return response.choices[0].message.content
        except Exception as e:
            return f"Error generating answer: {str(e)}"

    def _get_llm_answer_with_confidence_stream(self, prompt: str, stream_callback, debug_llm_payloads: bool = False) -> str:
        try:
            payload = {
                "model": config.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": config.MAX_TOKENS * 2,
                "temperature": 0.2,
                "stream": True,
            }
            if debug_llm_payloads:
                self.last_llm_payloads.append({
                    "stage": "final_answer_generation_stream",
                    "target_file": "backend/agents/UI_dashboard/prompts.py",
                    "target_function": "LightweightAnalyzer._get_llm_answer_with_confidence_stream",
                    "payload": payload,
                })
            response = self.client.chat.completions.create(
                **payload
            )
            full_text = ""
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    c = chunk.choices[0].delta.content
                    full_text += c
                    stream_callback(c)

            # Simple estimation for streaming tokens
            self.token_usage['input_tokens'] += len(prompt) // 4
            self.token_usage['output_tokens'] += len(full_text) // 4
            return full_text
        except Exception as e:
            err = f"Error in stream: {str(e)}"
            stream_callback(err)
            return err

    def _get_confidence_level(self, score: float) -> str:
        if score >= 80:
            return "🟢 High - 80%+ Accuracy"
        elif score >= 60:
            return "🟡 Medium - 60-80% Accuracy"
        else:
            return "🔴 Low - <60% Accuracy, Verify Independently"

