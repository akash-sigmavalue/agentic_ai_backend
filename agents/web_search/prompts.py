"""
LLM-based analysis - MINIMAL TOKEN USAGE
Only called when needed, uses GPT-4o-mini for cost efficiency
Token usage: 500-2000 per analysis
"""

import json
import re
from typing import List, Dict, Any, Optional
from datetime import datetime
from dataclasses import asdict
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
        source_traceability: Dict = None,
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
            if not self.client:
                answer = self.build_source_based_answer(query, results)
            elif stream_callback:
                answer = self._get_llm_answer_with_confidence_stream(
                    prompt,
                    lambda _chunk: None,
                    debug_llm_payloads=debug_llm_payloads,
                )
            else:
                answer = self._get_llm_answer_with_confidence(prompt, debug_llm_payloads=debug_llm_payloads)

            if not str(answer or "").strip() or str(answer).startswith("Error generating answer:"):
                answer = self.build_source_based_answer(query, results)

            # Post-process only for launch/construction project discovery, not every listing query.
            if self._is_new_project_query(query, intent) and hasattr(self, "validate_real_estate_content"):
                answer = self.validate_real_estate_content(answer, query)

            answer = self._replace_generic_source_link_labels(answer, results)
            answer = self._append_numbered_sources_to_uncited_lines(answer, results)
            answer = self._append_source_urls_to_cited_lines(answer, results)

            traceability_section = self.build_source_traceability_section(
                results,
                source_traceability=source_traceability,
            )
            if traceability_section and "## Source Traceability" not in str(answer):
                answer = f"{answer}\n\n{traceability_section}"
            if stream_callback:
                stream_callback(answer)

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
            fallback = self.build_source_based_answer(query, results)
            fallback = self._replace_generic_source_link_labels(fallback, results)
            fallback = self._append_numbered_sources_to_uncited_lines(fallback, results)
            fallback = self._append_source_urls_to_cited_lines(fallback, results)
            traceability_section = self.build_source_traceability_section(
                results,
                source_traceability=source_traceability,
            )
            if traceability_section and "## Source Traceability" not in str(fallback):
                fallback = f"{fallback}\n\n{traceability_section}"
            if stream_callback and fallback:
                stream_callback(fallback)
            return {
                'answer': fallback or f"An error occurred during analysis: {str(e)}",
                'accuracy_score': 0,
                'validated_claims': [],
                'sources_agreed': 0,
                'recommendation': "Error during validation",
                'confidence_level': "🔴 Error"
            }

    def build_source_based_answer(self, query: str, results: List[Dict]) -> str:
        """Build a deterministic answer from the most relevant retrieved sources."""
        if not results:
            return (
                "I could not find reliable source results for this query right now. "
                "Please try again with a more specific location, year, identifier, or source type."
            )

        exact_lines = []
        for idx, result in enumerate(results[:10], 1):
            for row in result.get("exact_ready_reckoner_rows", []) or []:
                text = row.get("row_text")
                if text:
                    exact_lines.append((idx, text, result))
            for match in result.get("exact_evidence_matches", []) or []:
                text = match.get("text")
                if text:
                    exact_lines.append((idx, text, result))

        answer = [f"## Source-Based Answer\n\nI found the most relevant available sources for: **{query}**."]

        if exact_lines:
            answer.append("\n\n## Exact Matches")
            for idx, text, result in exact_lines[:8]:
                source_url = result.get("url") or ""
                answer.append(f"\n- [{idx}] {text} Source: {source_url}")

        if self._is_existing_property_query(query):
            answer.append("\n\n## Matching Existing Projects / Listings")
            answer.append("\n| Source / Listing Page | Area / Match Signal | Details Found |")
            answer.append("\n|---|---|---|")
            for idx, result in enumerate(results[:10], 1):
                title = self._clean_text(result.get("title") or "Untitled source")
                snippet = self._clean_text(result.get("content") or result.get("snippet") or "")
                area_signal = self._match_signal_for_query(query, f"{title} {snippet} {result.get('url', '')}")
                details = snippet[:260] if snippet else "The source title matched the query, but detailed page text was not available."
                source_url = result.get("url") or ""
                answer.append(f"\n| [{idx}] {title} | {area_signal} | {details} Source: {source_url} |")
        else:
            answer.append("\n\n## Key Findings From Sources")
            for idx, result in enumerate(results[:10], 1):
                title = self._clean_text(result.get("title") or "Untitled source")
                snippet = self._clean_text(result.get("content") or result.get("snippet") or "")
                if not snippet:
                    snippet = "No detailed extract was available; use the URL to verify the source directly."
                source_url = result.get("url") or ""
                answer.append(f"\n### [{idx}] {title}\n{snippet[:600]}\n\nSource: {source_url}")

        answer.append("\n\n## Confidence Insight")
        if exact_lines:
            answer.append("\nThe answer includes exact evidence lines extracted from retrieved source content. Verify final decisions against the linked pages because live web listings, prices, rates, and regulations can change.")
        else:
            answer.append("\nThis is a source-based fallback answer from retrieved titles/snippets/content. It avoids inventing missing details; verify specifics on the linked pages.")

        answer.append("\n\n### Reference URLs")
        for idx, result in enumerate(results[:10], 1):
            title = self._clean_text(result.get("title") or result.get("url") or f"Source {idx}")
            url = result.get("url") or ""
            if url:
                answer.append(f"\n{idx}. [{title}]({url})")

        traceability_section = self.build_source_traceability_section(results)
        if traceability_section:
            answer.append(f"\n\n{traceability_section}")

        return "".join(answer)

    def _append_source_urls_to_cited_lines(self, answer: str, results: List[Dict]) -> str:
        """
        Display-only provenance pass: expand existing [1], [2] citations into exact URLs.
        This does not select sources, extract data, rank results, or change URL lists.
        """
        if not answer or not results:
            return answer or ""

        source_urls = {
            index: result.get("url")
            for index, result in enumerate(results[:10], 1)
            if result.get("url")
        }
        if not source_urls:
            return answer

        lines = []
        in_code_block = False
        skip_section = False
        for raw_line in str(answer).splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()

            if stripped.startswith("```"):
                in_code_block = not in_code_block
                lines.append(line)
                continue

            heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
            if heading_match:
                heading = heading_match.group(1).strip().lower()
                skip_section = heading in {
                    "reference urls",
                    "source traceability",
                    "top verified sources",
                    "additional sources used",
                    "crawled source urls",
                    "document source urls",
                    "extracted evidence lines",
                }
                lines.append(line)
                continue

            if in_code_block or skip_section or not stripped:
                lines.append(line)
                continue

            lines.append(self._append_source_urls_to_line(line, source_urls))

        return "\n".join(lines).strip()

    def _replace_generic_source_link_labels(self, text: str, results: List[Dict]) -> str:
        """Replace generic source links with their numbered result citation."""
        if not text or not results:
            return text or ""

        index_by_url = {
            str(result.get("url")).rstrip("/"): index
            for index, result in enumerate(results[:10], 1)
            if result.get("url")
        }
        if not index_by_url:
            return text

        def replacement(match) -> str:
            url = match.group("url")
            index = index_by_url.get(url.rstrip("/"))
            return f"[{index}]({url})" if index else match.group(0)

        return re.sub(
            r"\[(?:source(?:\s+url)?|url)\]\((?P<url>https?://[^\s)]+)\)",
            replacement,
            text,
            flags=re.IGNORECASE,
        )

    def _append_numbered_sources_to_uncited_lines(self, answer: str, results: List[Dict]) -> str:
        """Attach a numbered source link to uncited factual lines with matching evidence."""
        if not answer or not results:
            return answer or ""

        source_texts = []
        for index, result in enumerate(results[:10], 1):
            url = result.get("url")
            if not url:
                continue
            evidence = " ".join(
                str(result.get(field) or "")
                for field in ("title", "snippet", "content")
            ).lower()
            source_texts.append((index, url, evidence))

        if not source_texts:
            return answer

        lines = []
        in_code_block = False
        skip_section = False
        for raw_line in str(answer).splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                lines.append(line)
                continue

            heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
            if heading_match:
                heading = heading_match.group(1).strip().lower()
                skip_section = heading in {
                    "reference urls",
                    "source traceability",
                    "top verified sources",
                    "additional sources used",
                    "crawled source urls",
                    "document source urls",
                    "extracted evidence lines",
                }
                lines.append(line)
                continue

            if (
                in_code_block
                or skip_section
                or not stripped
                or re.search(r"\[\d+\](?:\(|\b)", line)
                or "http://" in line
                or "https://" in line
                or re.search(r"\bSource(?:s)?\s*:", line, re.IGNORECASE)
                or re.match(r"^\s*\d+\.\s+\S.*:\s*$", line)
                or re.match(r"^\s*\|?[-:| ]{3,}\|?\s*$", line)
            ):
                lines.append(line)
                continue

            best_source = self._best_matching_source_for_line(line, source_texts)
            if best_source:
                index, url = best_source
                line = f"{line} [{index}]({url})"
            lines.append(line)

        return "\n".join(lines).strip()

    def _best_matching_source_for_line(self, line: str, source_texts: List[tuple]) -> Optional[tuple]:
        stop_words = {
            "about", "after", "also", "been", "being", "from", "have", "into",
            "only", "same", "such", "that", "their", "there", "these", "this",
            "those", "used", "using", "were", "which", "with",
        }
        terms = {
            term.lower()
            for term in re.findall(r"[A-Za-z0-9]+", line)
            if (len(term) > 2 or term.isdigit()) and term.lower() not in stop_words
        }
        if not terms:
            return None

        numbers = {term for term in terms if any(char.isdigit() for char in term)}
        scored = []
        for index, url, evidence in source_texts:
            matched = {term for term in terms if term in evidence}
            matched_numbers = numbers & matched
            score = len(matched) + (len(matched_numbers) * 3)
            scored.append((score, len(matched_numbers), len(matched), index, url))

        score, number_matches, term_matches, index, url = max(scored)
        if number_matches or term_matches >= 2:
            return index, url
        return None

    def _append_source_urls_to_line(self, line: str, source_urls: Dict[int, str]) -> str:
        if not line.strip() or "http://" in line or "https://" in line or re.search(r"\bSource(?:s)?:", line, re.I):
            return line

        cited_indexes = []
        for match in re.findall(r"\[(\d+)\]", line):
            index = int(match)
            if index in source_urls and index not in cited_indexes:
                cited_indexes.append(index)

        if not cited_indexes:
            return line

        urls = [source_urls[index] for index in cited_indexes]
        source_text = "Source URL" if len(urls) == 1 else "Source URLs"
        suffix = f" {source_text}: {' | '.join(urls)}"

        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            return re.sub(r"\s*\|$", f"{suffix} |", line)

        return f"{line}{suffix}"

    def build_source_traceability_section(self, results: List[Dict], source_traceability: Dict = None) -> str:
        """Render source-level traceability without changing extraction/ranking behavior."""
        traceability = source_traceability or self._build_local_source_traceability(results)
        if not traceability:
            return ""

        lines = ["## Source Traceability"]

        verified_sources = traceability.get("top_verified_sources") or []
        additional_sources = traceability.get("additional_sources") or []
        lines.append("\n### Top Verified Sources")
        if verified_sources:
            for i, source in enumerate(verified_sources[:10], 1):
                title = self._clean_text(source.get("title") or source.get("url") or f"Source {i}")
                url = source.get("url") or ""
                trust = source.get("trust_score")
                trust_text = f"{float(trust) * 100:.0f}%" if isinstance(trust, (int, float)) else "unknown"
                # relevance = source.get("relevance_score")
                # relevance_text = f"{float(relevance) * 100:.0f}%" if isinstance(relevance, (int, float)) else "unknown"
                # lines.append(
                #    f"{i}. [{title}]({url}) - trust score: {trust_text}; relevance score: {relevance_text}"
                #)
                lines.append(f"{i}. [{title}]({url}) - trust score: {trust_text}")
        else:
            lines.append("No source met the current verified-source indicator check.")
            if additional_sources:
                lines.append("\n### Source URLs")
                for i, source in enumerate(additional_sources[:10], 1):
                    title = self._clean_text(source.get("title") or source.get("url") or f"Source {i}")
                    url = source.get("url") or ""
                    lines.append(f"{i}. [{title}]({url})")

        crawled_sources = traceability.get("crawled_source_urls") or []
        if crawled_sources:
            lines.append("\n### Crawled Source URLs")
            for i, source in enumerate(crawled_sources[:20], 1):
                title = self._clean_text(source.get("title") or source.get("url") or f"Crawled source {i}")
                url = source.get("url") or ""
                lines.append(f"{i}. [{title}]({url})")

        document_sources = traceability.get("document_source_urls") or []
        if document_sources:
            lines.append("\n### Document Source URLs")
            for i, source in enumerate(document_sources[:20], 1):
                title = self._clean_text(source.get("title") or source.get("url") or f"Document source {i}")
                url = source.get("url") or ""
                document = source.get("document") or {}
                source_url = document.get("source_url")
                if source_url and source_url != url:
                    lines.append(f"{i}. [{title}]({url}) from {source_url}")
                else:
                    lines.append(f"{i}. [{title}]({url})")

        return "\n".join(lines)

    def _build_local_source_traceability(self, results: List[Dict]) -> Dict:
        traceability = {
            "top_verified_sources": [],
            "additional_sources": [],
            "crawled_source_urls": [],
            "document_source_urls": [],
            "extracted_evidence": [],
        }
        for index, result in enumerate(results or [], 1):
            source_type = result.get("source") or "web"
            url = result.get("url")
            if not url:
                continue
            source_info = {
                "index": index,
                "title": result.get("title"),
                "url": url,
                "source": source_type,
                "trust_score": result.get("trust_score", result.get("source_trust", 0.5)),
                # "relevance_score": result.get("relevance_score"),
                "verification_status": result.get("verification_status", "unverified"),
                "reference_urls": result.get("reference_urls", []),
            }
            if source_type == "deep-crawl":
                traceability["crawled_source_urls"].append(source_info)
            elif source_type == "document-extraction":
                source_info["document"] = result.get("document")
                traceability["document_source_urls"].append(source_info)
            elif source_info["verification_status"] == "verified_indicator":
                traceability["top_verified_sources"].append(source_info)
            else:
                traceability["additional_sources"].append(source_info)

            for row in result.get("exact_ready_reckoner_rows", []) or []:
                text = row.get("row_text")
                if text:
                    traceability["extracted_evidence"].append({
                        "source_index": index,
                        "source_url": url,
                        "source_title": result.get("title"),
                        "type": "exact_ready_reckoner_row",
                        "text": text,
                    })
            for match in result.get("exact_evidence_matches", []) or []:
                text = match.get("text")
                if text:
                    traceability["extracted_evidence"].append({
                        "source_index": index,
                        "source_url": url,
                        "source_title": result.get("title"),
                        "type": "exact_evidence_match",
                        "text": text,
                    })
            traceability["extracted_evidence"].extend(
                self._structured_data_evidence(
                    result.get("extracted_data"),
                    index,
                    url,
                    result.get("title"),
                )
            )

        traceability["top_verified_sources"].sort(key=lambda item: item.get("trust_score") or 0, reverse=True)
        traceability["additional_sources"].sort(key=lambda item: item.get("trust_score") or 0, reverse=True)
        return traceability

    def _structured_data_evidence(self, extracted_data, source_index: int, source_url: str, source_title: str = None) -> List[Dict]:
        if not extracted_data:
            return []
        if hasattr(extracted_data, "__dataclass_fields__"):
            extracted_data = asdict(extracted_data)
        if not isinstance(extracted_data, dict):
            return []

        evidence = []
        for fact in extracted_data.get("key_facts") or []:
            if fact:
                evidence.append({
                    "source_index": source_index,
                    "source_url": source_url,
                    "source_title": source_title,
                    "type": "key_fact",
                    "text": str(fact),
                })

        for number in extracted_data.get("numbers") or []:
            if not isinstance(number, dict):
                continue
            value = number.get("value")
            context = number.get("context")
            text = f"{value}: {context}" if context else str(value or "")
            if text.strip():
                evidence.append({
                    "source_index": source_index,
                    "source_url": source_url,
                    "source_title": source_title,
                    "type": "number",
                    "text": text,
                })

        for field_name in ("dates", "locations", "entities"):
            for value in extracted_data.get(field_name) or []:
                if value:
                    evidence.append({
                        "source_index": source_index,
                        "source_url": source_url,
                        "source_title": source_title,
                        "type": field_name[:-1],
                        "text": str(value),
                    })

        return evidence

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _match_signal_for_query(self, query: str, text: str) -> str:
        query_lower = query.lower()
        text_lower = text.lower()
        signals = []
        for label, terms in {
            "Chicago": ["chicago"],
            "water/sea view": ["sea view", "waterfront", "lake view", "lake-view", "water view", "river view"],
            "existing/listing": ["existing", "for sale", "homes for sale", "real estate", "listing", "listings"],
        }.items():
            if any(term in query_lower for term in terms) and any(term in text_lower for term in terms):
                signals.append(label)
        return ", ".join(signals) or "Relevant source match"

    def _is_new_project_query(self, query: str, intent: str = None) -> bool:
        query_lower = query.lower()
        if intent == "construction_status":
            return False
        new_signals = [
            "new project", "new launch", "upcoming", "pre-launch", "pre launch",
            "under construction", "announced", "launching", "future project",
        ]
        existing_signals = [
            "existing", "available", "for sale", "resale", "ready to move",
            "ready-to-move", "homes for sale", "listing", "listings",
        ]
        return (
            "project" in query_lower
            and any(signal in query_lower for signal in new_signals)
            and not any(signal in query_lower for signal in existing_signals)
        )

    def _is_existing_property_query(self, query: str) -> bool:
        query_lower = query.lower()
        real_estate_terms = [
            "real estate", "project", "property", "properties", "home", "homes",
            "condo", "condos", "apartment", "apartments", "flat", "villa",
            "listing", "listings",
        ]
        existing_terms = [
            "existing", "available", "for sale", "resale", "ready to move",
            "ready-to-move", "sea view", "waterfront", "lake view", "lake-view",
            "river view", "view",
        ]
        return (
            any(term in query_lower for term in real_estate_terms)
            and any(term in query_lower for term in existing_terms)
            and not self._is_new_project_query(query)
        )

    def _is_location_specific_development_regulation_query(self, query: str, intent: str = None) -> bool:
        query_lower = query.lower()
        if intent == "development_regulation":
            return True
        regulatory_terms = [
            "fsi", "far", "floor space index", "floor area ratio",
            "development control", "dcr", "dcpr", "udcpr", "building rules",
            "building bye laws", "building bylaws", "land development",
            "land development rules", "land development regulations",
            "layout rules", "subdivision rules", "plot development",
            "plot layout", "zoning", "land use",
            "setback", "setbacks", "building height", "premium fsi",
            "fungible fsi", "tdr", "tod", "road width", "plot coverage",
            "planning authority", "development plan", "master plan",
        ]
        return any(term in query_lower for term in regulatory_terms)

    def _infer_location_from_query(self, query: str) -> str:
        match = re.search(r"\b(?:in|at|near|around)\s+([A-Za-z][A-Za-z\s,.-]{2,80})", query)
        if match:
            location = re.split(r"\b(?:with|for|and|that|which|having)\b", match.group(1), maxsplit=1)[0]
            return re.sub(r"\s+", " ", location).strip(" ,.-") or "the requested location"
        return "the requested location"

    def _trust_value(self, result: Dict) -> float:
        trust = result.get("trust_score", result.get("source_trust", 0.5))
        return trust if isinstance(trust, (int, float)) else 0.5

    def _rank_context_results(self, results: List[Dict], limit: int = 12) -> List:
        """Select sources whose content should be visible to the answer prompt."""
        def score(item):
            index, result = item
            source_type = result.get("source") or "web"
            has_content = bool(result.get("content") or result.get("snippet"))
            return (
                result.get("verification_status") == "verified_indicator",
                self._trust_value(result),
                source_type in {"deep-crawl", "document-extraction"},
                has_content,
                -index,
            )

        return sorted(enumerate(results or [], 1), key=score, reverse=True)[:limit]

    def _source_excerpt_for_query(self, content: str, query: str, intent: str = None, limit: int = 5000) -> str:
        """Prefer query-relevant chunks from long official pages/PDFs over only the first bytes."""
        content = str(content or "").strip()
        if len(content) <= limit:
            return content

        regulatory_terms = [
            "fsi", "far", "floor space index", "floor area ratio", "development control",
            "land development", "layout", "subdivision", "plot development", "zoning",
            "land use", "setback", "height", "road width", "open space", "amenity",
            "tdr", "tod", "premium", "municipal", "planning authority", "notification",
            "gazette", "rule", "regulation", "permission", "approval",
        ]
        query_terms = [
            term.lower()
            for term in re.findall(r"[A-Za-z0-9]+", query or "")
            if len(term) > 2
        ]
        terms = list(dict.fromkeys(query_terms + regulatory_terms))

        raw_chunks = re.split(r"(?:\n\s*){1,}|(?<=[.;:])\s+(?=[A-Z0-9])", content)
        chunks = []
        for index, chunk in enumerate(raw_chunks):
            text = re.sub(r"\s+", " ", chunk).strip()
            if len(text) < 30:
                continue
            text_lower = text.lower()
            hits = sum(1 for term in terms if term in text_lower)
            has_number = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|m|meter|metre|sq\.?m|sqm|acre|hectare|ha|ft|feet)?", text_lower))
            official_hit = any(term in text_lower for term in ["official", "government", "municipal", "authority", "gazette", "notification"])
            score = (hits * 3) + (2 if has_number else 0) + (2 if official_hit else 0)
            if score > 0:
                chunks.append((score, index, text[:900]))

        if not chunks:
            return content[:limit]

        chunks.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected = sorted(chunks[:12], key=lambda item: item[1])
        excerpt_parts = []
        total = 0
        for _score, _index, text in selected:
            if total + len(text) + 2 > limit:
                break
            excerpt_parts.append(text)
            total += len(text) + 2

        return "\n".join(excerpt_parts).strip() or content[:limit]

    def _build_accuracy_prompt(self, query: str, results: List[Dict], validation: Dict, intent: str = None) -> str:
        source_context = []
        source_priority_context = []
        exact_row_context = []
        exact_evidence_context = []
        prioritized_sources = self._rank_context_results(results)
        for i, r in prioritized_sources:
            trust_score = self._trust_value(r)
            source_priority_context.append(
                f"[{i}] {r.get('title')}\nURL: {r.get('url')}\nTrust Score: {trust_score*100:.0f}%\nVerification Status: {r.get('verification_status', 'unverified')}\nSource Type: {r.get('source') or 'web'}"
            )
        for i, r in prioritized_sources:
            raw_content = r.get('content') or r.get('snippet') or "No content available."
            content = self._source_excerpt_for_query(raw_content, query, intent)
            source_type = r.get('source') or "web"
            document = r.get('document') or {}
            doc_line = f"\nDocument: {document.get('filename')} from {document.get('source_url')}" if document else ""
            trust_score = self._trust_value(r)
            source_context.append(f"[{i}] {r.get('title')}\nSource Type: {source_type}{doc_line}\nURL: {r.get('url')}\nTrust Score: {trust_score*100:.0f}%\nContent: {content[:5000]}")
            for row in r.get('exact_ready_reckoner_rows', []) or []:
                exact_row_context.append(f"[{i}] {row.get('row_text')} | Exact Source URL: {r.get('url')}")
            for match in r.get('exact_evidence_matches', []) or []:
                exact_evidence_context.append(f"[{i}] {match.get('text')} | Exact Source URL: {r.get('url')}")
        validated_str = "\n".join([f"- {c['claim']} (Verified by {c['source_count']} sources)" for c in validation.get('validated_claims', [])])
        sources_str = "\n\n".join(source_context)

        if self._is_location_specific_development_regulation_query(query, intent):
            location = self._infer_location_from_query(query)

            return f"""You are a municipal planning and development-regulation research assistant.

User Query: {query}

This query is about location-specific development rules such as land development rules, layout/subdivision rules, plot development rules, FSI/FAR, Development Control Regulations, zoning, setbacks, building height, land use, TDR, TOD, premium FSI, or road-width/plot-size dependent rules.

CRITICAL JURISDICTION RULES:
- Do not provide a generic FSI/FAR, zoning, setback, height, or land-use answer.
- First identify the exact jurisdiction: country, state, district/city, municipal corporation or municipality, planning/development authority, district/taluka/village, ward/zone/sector if available.
- If the user gives only a broad city/locality, explain that final applicability may differ by municipal corporation, planning authority, cantonment, MIDC/special planning area, PMRDA/MMRDA/metropolitan authority, heritage/coastal/airport/defense/forest/flood restrictions, zone, road width, plot size, and land use.
- Prioritize official sources in this order: state urban development/town planning department, municipal corporation/planning authority, official DCR/DCPR/UDCPR/master plan/development plan, gazette notifications, amendments, GRs, and circulars.
- Use secondary real-estate/blog/consultant sources only as non-authoritative clues, never as the basis for a final rule.
- Check and report document name, effective date, notification/amendment date, issuing authority, and whether the rule appears current in the available sources.
- For land development rules, separately cover layout/subdivision approval, minimum plot size, road width/internal road requirements, open space/amenity space, reservations, land-use/zoning, conversion/NA permission, access requirements, infrastructure/service rules, and special planning-area restrictions when source-backed.
- For FSI/FAR, separately cover base FSI, premium/purchasable FSI, TDR, TOD, road-width dependency, land-use dependency, plot-size dependency, and special restrictions when source-backed.
- If the official source context does not contain the exact jurisdiction or rule detail, say: "I cannot give the exact applicable rule from the available official sources without the missing jurisdiction/plot details."
- Ask for missing plot-level inputs when needed: exact municipal authority, plot address/village/ward/zone, plot area, road width, land use, survey/CTS/plot number, redevelopment/new construction/special scheme.
- Every factual statement must include a citation [n] and exact source URL beside the statement.
- Choose evidence from VERIFIED SOURCE PRIORITY in order. If higher-trust sources do not contain the needed detail, say so before using lower-trust context.

LOCATION FOCUS INFERRED FROM QUERY:
{location}

EXACT EVIDENCE MATCHES:
{chr(10).join(exact_evidence_context) if exact_evidence_context else "No exact regulation-level evidence was extracted."}

CROSS-SOURCE VALIDATION DATA:
{validated_str or "No cross-source consensus found for specific regulatory values."}

VERIFIED SOURCE PRIORITY (highest trust first; use this sequence when choosing evidence):
{chr(10).join(source_priority_context) if source_priority_context else "No source priority metadata available."}

SEARCH RESULTS CONTEXT:
{"-"*20}
{sources_str}
{"-"*20}

FORMAT:
## Jurisdiction Check
State exactly which jurisdiction/planning authority the sources support, and what is still missing.

## Rule Document Used
List official document/notification names, issuing authority, effective dates, amendment dates, and exact source URLs.

## Applicable Rule Summary
Use a table with columns: Topic, Source-backed rule/value, Applicability conditions, Missing details, Source URL.

## Land Development Rule Components
If relevant, separately cover layout/subdivision approval, minimum plot size, road width/internal roads, open space/amenity space, reservations, land-use/zoning, conversion/NA permission, access, infrastructure/service conditions, and special planning-area restrictions. Say "not verified in available official source context" where missing.

## FSI/FAR Components
If relevant, separately cover Base FSI, Premium/Purchasable FSI, TDR, TOD, road-width dependency, land-use dependency, plot-size dependency, and special restrictions. Say "not verified in available official source context" where missing.

## Required Verification
List the exact clarification questions or authority checks needed before relying on the answer for a plot.

## Confidence Insight
State whether the answer is High/Medium/Low confidence based only on official-source coverage and exact jurisdiction match.

### Reference URLs
Include clickable markdown links [Title](URL) for the first 10 provided sources.

Answer:"""

        if self._is_existing_property_query(query):
            location = self._infer_location_from_query(query)

            return f"""You are a real estate research analyst.

User Query: {query}

The user is asking for existing real estate projects/listings, not only new launches. Use the retrieved sources to produce a descriptive answer before listing URLs.

CRITICAL INSTRUCTIONS:
- Treat "sea view" in inland/lake cities as waterfront, lake-view, river-view, or water-view when sources use those terms.
- Include existing properties/projects, communities, condos, apartments, and homes when they match the user's location and view requirement.
- Do not answer with only a fallback sentence if source titles/snippets clearly show matching listings.
- For each item, provide the name/title, area/neighborhood, property type if available, what makes it match the requested view, and key details available from the source.
- If sources are listing-search pages rather than individual project pages, summarize what each source indicates and say individual availability should be verified on the linked page.
- Do not invent prices, unit counts, builders, or amenities not present in the source context.
- Every extracted statement, row, listing detail, and data point must include its exact source URL in the same row or bullet.
- If a complete paragraph is extracted from one source, mention that exact source URL at the end of that paragraph.
- Do not reduce or shorten data extraction to make room for URLs. Keep the same detailed extraction depth and add source URLs beside the extracted data.
- Extract all relevant details available in the source context: names, locations, rates, dates, amounts, official bodies, status, conditions, exceptions, missing fields, and caveats.
- Do not produce short sections. When source context contains enough material, use detailed paragraphs plus multiple bullets or table rows, not one-line summaries.
- Choose evidence from VERIFIED SOURCE PRIORITY in order: first use the highest-trust verified source; if it does not contain relevant data, move to the next source in that priority list.

LOCATION FOCUS:
{location}

EXACT EVIDENCE MATCHES:
{chr(10).join(exact_evidence_context) if exact_evidence_context else "No exact constraint-level evidence was extracted."}

VERIFIED SOURCE PRIORITY (highest trust first; use this sequence when choosing evidence):
{chr(10).join(source_priority_context) if source_priority_context else "No source priority metadata available."}

SEARCH RESULTS CONTEXT:
{"-"*20}
{sources_str}
{"-"*20}

FORMAT:
## Executive Summary
Explain what matching existing sea/water-view real estate was found with source-backed specifics.

## Matching Existing Projects / Listings
Use a table with columns: Name / Source Title, Area, Type, View Match, Details Found, Source.

## Notes And Gaps
Mention missing details or where the source only provides a listing collection.

### Reference URLs
Include clickable markdown links [Title](URL) for the first 10 provided sources.
Every extracted statement must include the exact source URL it came from.

Answer:"""

        if self._is_new_project_query(query, intent):
            year_match = re.search(r'20\d{2}', query)
            year = year_match.group(0) if year_match else '2026'
            location = self._infer_location_from_query(query)

            return f"""You are a real estate market analyst.

## CRITICAL RULES - STRICTLY FOLLOW:

1. **ONLY** provide information about REAL ESTATE PROJECTS (residential/commercial flats, apartments, villas, plots)
2. **NEVER** mention tourist attractions, restaurants, hotels, or places to visit
3. **NEVER** use Tripadvisor, travel sites, or tourism sources
4. **ALWAYS** prioritize official registration, developer disclosures, and sources with direct evidence
5. **ALWAYS** include for each project: name, builder, location, status, possession date
6. **ALWAYS** include the exact source URL beside every extracted project detail or table row
7. **ALWAYS** choose evidence from the verified source priority list in order, using the next source only when the higher-trust source does not contain the relevant data
8. **ALWAYS** mention the exact source URL at the end of a paragraph when the full paragraph comes from one source
9. **NEVER** shorten data extraction because source URLs are required; preserve detailed extraction and append URLs beside the relevant extracted line/data point
10. **ALWAYS** extract every relevant available field from the source context instead of giving only a short summary
11. **ALWAYS** use detailed rows/bullets when source context has enough material; do not collapse project extraction to only highlights

## User Query: {query}

## Available Sources (Real Estate Only):
Verified source priority, highest trust first:
{chr(10).join(source_priority_context) if source_priority_context else "No source priority metadata available."}

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
- Include the exact source URL beside every extracted project detail or table row
- If a complete paragraph is extracted from one source, mention that exact source URL at the end of that paragraph.
- Do not reduce or shorten data extraction to make room for URLs. Keep the same detailed extraction depth and add source URLs beside the extracted data.
- Extract all relevant details available in the source context: project names, builders, locations, possession dates, prices, units, registration numbers, official status, caveats, and missing fields.
- Do not produce short sections. When source context contains enough material, use detailed paragraphs plus multiple bullets or table rows, not one-line summaries.
- Choose evidence from VERIFIED SOURCE PRIORITY in order: first use the highest-trust verified source; if it does not contain relevant project data, move to the next source in that priority list.

CROSS-SOURCE VALIDATION DATA:
{validated_str or "No cross-source consensus found."}

VERIFIED SOURCE PRIORITY (highest trust first; use this sequence when choosing evidence):
{chr(10).join(source_priority_context) if source_priority_context else "No source priority metadata available."}

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
EXACT ROW-LEVEL MATCHES:
{chr(10).join(exact_row_context) if exact_row_context else "No exact survey/row-level match was extracted."}
EXACT EVIDENCE MATCHES:
{chr(10).join(exact_evidence_context) if exact_evidence_context else "No exact constraint-level evidence was extracted."}
CROSS-SOURCE VALIDATION DATA:
{validated_str or "No cross-source consensus found for specific numbers/facts."}

VERIFIED SOURCE PRIORITY (highest trust first; use this sequence when choosing evidence):
{chr(10).join(source_priority_context) if source_priority_context else "No source priority metadata available."}

SEARCH RESULTS CONTEXT:
{"-"*20}
{sources_str}
{"-"*20}

INSTRUCTIONS:
0. If EXACT ROW-LEVEL MATCHES contains a row for the requested survey number, answer from that row first and do not replace it with general locality rates.
1. If EXACT EVIDENCE MATCHES contains lines that directly answer the user query, answer from those lines first before using broader source context.
2. Preserve the user's exact constraints: location, year, ID/code/number, product/model/version, person/company, legal section/rule, date range, and quoted phrases.
3. If sources do not contain the requested specific constraint, say that clearly instead of giving a generic answer.
4. Provide an EXTREMELY DETAILED and COMPREHENSIVE answer (aim for 90% extraction of all relevant facts).
4a. For legal/regulatory applicability questions, extract the regulation's scope, applicable jurisdictions, non-applicable/excluded jurisdictions, exempted authorities, special planning areas, exceptions, and source wording that supports each item. Distinguish directly stated exclusions from inferred exclusions.
4b. Do not reduce, compress, or shorten data extraction because source URLs are required. Keep the answer detailed, relevant, exact, and accurate, then append the exact URL beside each extracted line/data point.
4c. Extract all available relevant facts from SEARCH RESULTS CONTEXT, including rates, dates, amounts, departments, official bodies, applicability, exceptions, missing fields, caveats, and conflicts. Do not stop after one or two bullets when more relevant source-backed details are present.
4d. Minimum detail requirement: if the source context has enough relevant material, provide at least 10-15 substantive extracted bullets/table rows across the findings and context sections. Each bullet/row must include its citation and exact source URL.
4e. For rate/regulatory/property questions, include separate detailed coverage for direct answer, exact rate/value, year/date applicability, issuing authority, calculation/use case, historical/comparison context, update frequency, applicability scope, exceptions/missing data, conflicts, and verification caveats when present in sources.
4f. Avoid generic filler. Prefer exact extracted wording, numbers, years, departments, rates, dates, and source-backed conditions over broad summaries.
5. Structure your response with high-density information:
   - ## 📝 Executive Summary
   - ## 🔍 Exhaustive Findings (Extract EVERY specific number, fee, date, and legal rule found)
   - ## ⚖️ Legal/Regulatory Context (Specific acts, sections, and official departments)
   - ## ⚠️ Penalties & Enforcement (If applicable, extract specific amounts and durations)
6. Use internal citations [1], [2], etc., for EVERY factual claim.
6a. Also include the exact source URL beside every extracted statement, line, row, or data point.
6b. Choose evidence from VERIFIED SOURCE PRIORITY in order: first use the highest-trust verified source; if it does not contain relevant data, move to the next source in that priority list.
6c. If a complete paragraph is extracted from one source, mention that exact source URL at the end of that paragraph.
7. DO NOT SUMMARIZE. If a source provides a list of 5 changes, list all 5 with their exact values.
8. If sources conflict, explicitly mention the contradiction.
9. Use tables to present numerical data or comparisons.
10. End with a "Confidence Insight" section.
11. CRITICAL: At the very end, add a section "### Reference URLs" with clickable markdown links [Title](URL) for the first 10 provided sources. Do not stop at 5 when 10 sources are available.

Answer:"""

    def _get_llm_answer_with_confidence(self, prompt: str, debug_llm_payloads: bool = False) -> str:
        try:
            payload = {
                "model": config.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self._answer_max_tokens(),
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
                "max_tokens": self._answer_max_tokens(),
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

    def _answer_max_tokens(self) -> int:
        """Use a larger answer budget so source URLs do not force short extraction."""
        return max(config.MAX_TOKENS * 4, 3000)

    def _get_confidence_level(self, score: float) -> str:
        if score >= 80:
            return "🟢 High - 80%+ Accuracy"
        elif score >= 60:
            return "🟡 Medium - 60-80% Accuracy"
        else:
            return "🔴 Low - <60% Accuracy, Verify Independently"

