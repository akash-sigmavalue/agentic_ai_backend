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
                if stream_callback:
                    stream_callback(answer)
            elif stream_callback:
                answer = self._get_llm_answer_with_confidence_stream(
                    prompt,
                    stream_callback,
                    debug_llm_payloads=debug_llm_payloads,
                )
            else:
                answer = self._get_llm_answer_with_confidence(prompt, debug_llm_payloads=debug_llm_payloads)

            if not str(answer or "").strip() or str(answer).startswith("Error generating answer:"):
                answer = self.build_source_based_answer(query, results)
                if stream_callback:
                    stream_callback(answer)

            # Post-process only for launch/construction project discovery, not every listing query.
            if self._is_new_project_query(query, intent) and hasattr(self, "validate_real_estate_content"):
                answer = self.validate_real_estate_content(answer, query)

            answer = self._add_sentence_source_labels(answer, results)

            traceability_section = self.build_source_traceability_section(
                results,
                source_traceability=source_traceability,
            )
            if traceability_section and "## Source Traceability" not in str(answer):
                answer = f"{answer}\n\n{traceability_section}"
                if stream_callback:
                    stream_callback(f"\n\n{traceability_section}")

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
            fallback = self._add_sentence_source_labels(fallback, results)
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

        return self._add_sentence_source_labels("".join(answer), results)

    def _add_sentence_source_labels(self, answer: str, results: List[Dict]) -> str:
        """
        Prefix factual prose sentences with the most likely retrieved content source.

        The LLM is instructed to cite every claim, but this pass makes the UI more
        dependable by showing "Content N" labels and one de-duplicated external
        source list even when the model omits a URL on a sentence.
        """
        if not answer or not results:
            return answer or ""

        sources = self._content_sources(results)
        if not sources:
            return answer

        body = self._strip_generated_source_sections(answer)
        annotated = self._annotate_markdown_sentences(body, sources)
        source_map = self._render_content_source_map(sources)
        external_sources = self._render_external_sources(results, sources)

        sections = [annotated.rstrip(), source_map]
        if external_sources:
            sections.append(external_sources)
        return "\n\n".join(section for section in sections if section).strip()

    def _content_sources(self, results: List[Dict]) -> List[Dict]:
        sources = []
        seen_urls = set()
        for index, result in enumerate(results or [], 1):
            url = result.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            text = " ".join([
                str(result.get("title") or ""),
                str(result.get("snippet") or ""),
                str(result.get("content") or ""),
            ])
            sources.append({
                "index": index,
                "label": f"Content {index}",
                "title": self._clean_text(result.get("title") or url),
                "url": url,
                "tokens": self._meaningful_tokens(text),
            })
        return sources

    def _strip_generated_source_sections(self, answer: str) -> str:
        section_pattern = re.compile(
            r"\n{0,2}(?:#{2,3}\s*)?(?:Content Source Map|External Sources|Reference URLs)\b.*?(?=\n#{2,3}\s|\Z)",
            re.IGNORECASE | re.DOTALL,
        )
        return section_pattern.sub("", answer).strip()

    def _annotate_markdown_sentences(self, answer: str, sources: List[Dict]) -> str:
        lines = []
        in_code_block = False
        for raw_line in str(answer or "").splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()

            if stripped.startswith("```"):
                in_code_block = not in_code_block
                lines.append(line)
                continue

            if (
                in_code_block
                or not stripped
                or stripped.startswith("#")
                or stripped.startswith("|")
                or re.match(r"^\|?[-:\s|]+\|?$", stripped)
                or re.match(r"^\s*(?:[-*]\s*)?\[(?:Content\s+\d+|Synthesis)\]", stripped, re.IGNORECASE)
                or re.match(r"^\s*\d+\.\s*\[.*?\]\(https?://", stripped)
            ):
                lines.append(line)
                continue

            prefix = ""
            content = line
            list_match = re.match(r"^(\s*(?:[-*]|\d+\.)\s+)(.*)$", line)
            if list_match:
                prefix, content = list_match.groups()

            annotated_content = self._annotate_sentence_text(content, sources)
            lines.append(f"{prefix}{annotated_content}" if prefix else annotated_content)

        return "\n".join(lines).strip()

    def _annotate_sentence_text(self, text: str, sources: List[Dict]) -> str:
        chunks = re.split(r"(?<=[.!?])(\s+)", text)
        if len(chunks) == 1:
            label = self._source_label_for_sentence(text, sources)
            return f"[{label}] {text.strip()}" if text.strip() else text

        annotated = []
        for index in range(0, len(chunks), 2):
            sentence = chunks[index]
            spacing = chunks[index + 1] if index + 1 < len(chunks) else ""
            if sentence.strip():
                label = self._source_label_for_sentence(sentence, sources)
                annotated.append(f"[{label}] {sentence.strip()}{spacing}")
            else:
                annotated.append(sentence + spacing)
        return "".join(annotated).rstrip()

    def _source_label_for_sentence(self, sentence: str, sources: List[Dict]) -> str:
        source_indexes = self._source_indexes_from_citations(sentence, sources)
        if source_indexes:
            return ", ".join(f"Content {index}" for index in source_indexes[:3])

        sentence_tokens = self._meaningful_tokens(sentence)
        if not sentence_tokens:
            return "Synthesis"

        best_source = None
        best_score = 0
        for source in sources:
            score = len(sentence_tokens.intersection(source["tokens"]))
            if score > best_score:
                best_source = source
                best_score = score

        if best_source and best_score > 0:
            return best_source["label"]
        return "Synthesis"

    def _source_indexes_from_citations(self, sentence: str, sources: List[Dict]) -> List[int]:
        valid_indexes = {source["index"] for source in sources}
        indexes = []
        for match in re.findall(r"\[(\d+)\]", sentence):
            index = int(match)
            if index in valid_indexes and index not in indexes:
                indexes.append(index)

        for source in sources:
            if source["url"] and source["url"] in sentence and source["index"] not in indexes:
                indexes.append(source["index"])

        return indexes

    def _meaningful_tokens(self, text: str) -> set:
        stop_words = {
            "about", "after", "also", "and", "are", "because", "been", "but",
            "can", "could", "does", "for", "from", "has", "have", "into",
            "its", "may", "more", "not", "only", "or", "such", "than", "that",
            "the", "their", "these", "this", "through", "to", "was", "were",
            "which", "with", "within", "without", "would", "you", "your",
        }
        return {
            token
            for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9.-]{2,}", str(text or "").lower())
            if token not in stop_words
        }

    def _render_content_source_map(self, sources: List[Dict]) -> str:
        lines = ["### Content Source Map"]
        for source in sources[:10]:
            lines.append(f"- [{source['label']}] [{source['title']}]({source['url']})")
        return "\n".join(lines)

    def _render_external_sources(self, results: List[Dict], sources: List[Dict]) -> str:
        primary_urls = {source["url"] for source in sources}
        external_urls = []
        for result in results or []:
            candidates = []
            candidates.extend(result.get("reference_urls") or [])
            document = result.get("document") or {}
            if document.get("source_url"):
                candidates.append(document["source_url"])
            candidates.extend(document.get("reference_urls") or [])

            for url in candidates:
                if url and url not in primary_urls and url not in external_urls:
                    external_urls.append(url)

        if not external_urls:
            return ""

        lines = ["### External Sources"]
        for index, url in enumerate(external_urls[:20], 1):
            lines.append(f"{index}. {url}")
        return "\n".join(lines)

    def build_source_traceability_section(self, results: List[Dict], source_traceability: Dict = None) -> str:
        """Render source-level traceability without changing extraction/ranking behavior."""
        traceability = source_traceability or self._build_local_source_traceability(results)
        if not traceability:
            return ""

        lines = ["## Source Traceability"]

        verified_sources = traceability.get("top_verified_sources") or []
        lines.append("\n### Top Verified Sources")
        if verified_sources:
            for i, source in enumerate(verified_sources[:10], 1):
                title = self._clean_text(source.get("title") or source.get("url") or f"Source {i}")
                url = source.get("url") or ""
                trust = source.get("trust_score")
                trust_text = f"{float(trust) * 100:.0f}%" if isinstance(trust, (int, float)) else "unknown"
                lines.append(f"{i}. [{title}]({url}) - trust score: {trust_text}")
        else:
            lines.append("No source met the current verified-source indicator check.")

        additional_sources = traceability.get("additional_sources") or []
        if additional_sources:
            lines.append("\n### Additional Sources Used")
            for i, source in enumerate(additional_sources[:10], 1):
                title = self._clean_text(source.get("title") or source.get("url") or f"Additional source {i}")
                url = source.get("url") or ""
                trust = source.get("trust_score")
                trust_text = f"{float(trust) * 100:.0f}%" if isinstance(trust, (int, float)) else "unknown"
                lines.append(f"{i}. [{title}]({url}) - trust score: {trust_text}")

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

        evidence_lines = traceability.get("extracted_evidence") or []
        if evidence_lines:
            lines.append("\n### Extracted Evidence Lines")
            for i, evidence in enumerate(evidence_lines[:20], 1):
                text = self._clean_text(evidence.get("text") or "")
                url = evidence.get("source_url") or ""
                source_index = evidence.get("source_index")
                lines.append(f"{i}. [Source {source_index}] {text} Source: {url}")

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

        traceability["top_verified_sources"].sort(key=lambda item: item.get("trust_score") or 0, reverse=True)
        traceability["additional_sources"].sort(key=lambda item: item.get("trust_score") or 0, reverse=True)
        return traceability

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

    def _infer_location_from_query(self, query: str) -> str:
        match = re.search(r"\b(?:in|at|near|around)\s+([A-Za-z][A-Za-z\s,.-]{2,80})", query)
        if match:
            location = re.split(r"\b(?:with|for|and|that|which|having)\b", match.group(1), maxsplit=1)[0]
            return re.sub(r"\s+", " ", location).strip(" ,.-") or "the requested location"
        return "the requested location"

    def _build_accuracy_prompt(self, query: str, results: List[Dict], validation: Dict, intent: str = None) -> str:
        source_context = []
        exact_row_context = []
        exact_evidence_context = []
        for i, r in enumerate(results[:10], 1):
            content = r.get('content') or r.get('snippet') or "No content available."
            source_type = r.get('source') or "web"
            document = r.get('document') or {}
            doc_line = f"\nDocument: {document.get('filename')} from {document.get('source_url')}" if document else ""
            trust_score = r.get('trust_score', r.get('source_trust', 0.5)) or 0.5
            source_context.append(f"[{i}] {r.get('title')}\nSource Type: {source_type}{doc_line}\nURL: {r.get('url')}\nTrust Score: {trust_score*100:.0f}%\nContent: {content[:5000]}")
            for row in r.get('exact_ready_reckoner_rows', []) or []:
                exact_row_context.append(f"[{i}] {row.get('row_text')} | Exact Source URL: {r.get('url')}")
            for match in r.get('exact_evidence_matches', []) or []:
                exact_evidence_context.append(f"[{i}] {match.get('text')} | Exact Source URL: {r.get('url')}")
        validated_str = "\n".join([f"- {c['claim']} (Verified by {c['source_count']} sources)" for c in validation.get('validated_claims', [])])
        sources_str = "\n\n".join(source_context)

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
- Treat SEARCH RESULTS CONTEXT entry [1] as Content 1, [2] as Content 2, and so on. Start every factual sentence with [Content N] or [Content N, Content M].

LOCATION FOCUS:
{location}

EXACT EVIDENCE MATCHES:
{chr(10).join(exact_evidence_context) if exact_evidence_context else "No exact constraint-level evidence was extracted."}

SEARCH RESULTS CONTEXT:
{"-"*20}
{sources_str}
{"-"*20}

FORMAT:
## Executive Summary
Briefly explain what matching existing sea/water-view real estate was found.

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
7. Treat source [1] as Content 1, [2] as Content 2, and so on. Start every factual sentence with [Content N] or [Content N, Content M].

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
- Include the exact source URL beside every extracted project detail or table row
- Treat SEARCH RESULTS CONTEXT entry [1] as Content 1, [2] as Content 2, and so on. Start every factual sentence with [Content N] or [Content N, Content M].

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
EXACT ROW-LEVEL MATCHES:
{chr(10).join(exact_row_context) if exact_row_context else "No exact survey/row-level match was extracted."}
EXACT EVIDENCE MATCHES:
{chr(10).join(exact_evidence_context) if exact_evidence_context else "No exact constraint-level evidence was extracted."}
CROSS-SOURCE VALIDATION DATA:
{validated_str or "No cross-source consensus found for specific numbers/facts."}

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
4b. Treat SEARCH RESULTS CONTEXT entry [1] as Content 1, [2] as Content 2, and so on. Start EVERY factual sentence with [Content N] or [Content N, Content M]. Use [Synthesis] only for reasoning that combines sources and is not directly copied from one source.
5. Structure your response with high-density information:
   - ## 📝 Executive Summary
   - ## 🔍 Exhaustive Findings (Extract EVERY specific number, fee, date, and legal rule found)
   - ## ⚖️ Legal/Regulatory Context (Specific acts, sections, and official departments)
   - ## ⚠️ Penalties & Enforcement (If applicable, extract specific amounts and durations)
6. Use internal citations [1], [2], etc., for EVERY factual claim.
6a. Also include the exact source URL beside every extracted statement, line, row, or data point.
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

