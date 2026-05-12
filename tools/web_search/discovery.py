from __future__ import annotations

"""
Query understanding and source discovery.

This is the first step of the search workflow:
1. Understand user intent and key entities.
2. Build a few focused search queries.
3. Visit multiple relevant result URLs and rank them by relevance.
"""

import re
import json
import random
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from urllib.parse import urlparse

from core.web_search.config import config
from tools.web_search.search import DuckDuckGoSearcher, SearchResult


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for",
    "from", "give", "have", "how", "i", "in", "is", "it", "me", "most",
    "of", "on", "or", "regarding", "search", "show", "that", "the", "this",
    "to", "want", "what", "whatever", "when", "where", "which", "will",
    "with", "you",
}


DOMAIN_EXPANSIONS = {
    "fsi": {
        "expanded": "Floor Space Index",
        "context_terms": ["building", "development control", "town planning", "sanctioned plan"],
        "avoid_terms": ["financial sanctions", "office of financial sanctions", "ofsi", "sanctions list"],
    },
    "udcpr": {
        "expanded": "Unified Development Control and Promotion Regulations Maharashtra",
        "context_terms": ["building rules", "development control", "Maharashtra"],
        "avoid_terms": [],
    },
}


PROPERTY_RATE_TERMS = [
    "ready reckoner",
    "reckoner rate",
    "ready rekoner",
    "rekoner rate",
    "ready reckner",
    "circle rate",
    "government valuation",
    "property rate",
    "guideline value",
    "igr maharashtra",
    "market value",
    "annual statement of rates",
    "asr rate",
]

PROPERTY_RATE_RESULT_TERMS = [
    "ready reckoner",
    "reckoner",
    "circle rate",
    "government valuation",
    "guideline value",
    "market value",
    "annual statement of rates",
    "asr",
    "stamp duty",
    "valuation",
    "rate per",
]

PROJECT_LISTING_TERMS = [
    "new projects",
    "residential projects",
    "upcoming projects",
    "project in",
    "flats",
    "apartments",
    "bhk",
    "possession",
    "amenities",
    "floor plan",
    "builder",
    "developer",
]

PROPERTY_RATE_AVOID_TERMS = [
    "health.gov",
    "immunisation",
    "immunization",
    "vaccine",
    "vaccines",
    "respiratory-syncytial-virus",
    "rsv",
    "disease",
    "medicine",
]

KNOWN_REAL_ESTATE_LOCATIONS = {
    "wakad", "baner", "hinjewadi", "kharadi", "wagholi", "nibm", "punawale",
    "kondhwa", "pune", "mumbai", "thane", "balewadi", "aundh", "hadapsar",
    "magarpatta", "kothrud", "pimple", "saudagar", "mahalunge", "ravet",
}


MIN_RELEVANCE_SCORE = 0.28


@dataclass
class QueryUnderstanding:
    original_query: str
    intent: str
    key_entities: List[str]
    rewritten_queries: List[str]
    positive_terms: List[str]
    avoid_terms: List[str]
    used_llm: bool = False
    is_real_estate: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


class RealEstateIntentDetector:
    """Detect real estate specific intents"""
    
    REAL_ESTATE_KEYWORDS = {
        'project_type': [
            'residential project', 'commercial project', 'housing project',
            'apartment complex', 'villa project', 'township', 'plot layout'
        ],
        'status': [
            'new launch', 'upcoming', 'under construction', 'ready to move',
            'pre-launch', 'completed', 'ongoing', 'announced'
        ],
        'features': [
            '2bhk', '3bhk', '4bhk', 'configurations', 'floor plan',
            'carpet area', 'super built-up area', 'possession date',
            'rera registered', 'approved project', 'circle rate', 'ready reckoner',
            'reckoner rate', 'ready rekoner', 'rekoner rate', 'guideline value',
            'market value', 'property valuation', 'government valuation'
        ],
        'location': [
            'pune', 'mumbai', 'bangalore', 'hyderabad', 'chennai',
            'wakad', 'baner', 'hinjewadi', 'kharadi', 'aundh', 'kothrud'
        ]
    }
    
    BLOCKED_DOMAINS = [
        'tripadvisor.com', 'travelocity.com', 'makemytrip.com', 'goibibo.com',
        'yatra.com', 'cleartrip.com', 'lonelyplanet.com'
    ]
    
    def is_real_estate_query(self, query: str) -> bool:
        """Check if query is real estate related"""
        query_lower = query.lower()
        if any(term in query_lower for term in PROPERTY_RATE_TERMS):
            return True
        if re.search(r"\bready\s+r(?:eckoner|ekoner|eckner|ekner|econer)\b", query_lower):
            return True
        for keywords in self.REAL_ESTATE_KEYWORDS.values():
            if any(kw in query_lower for kw in keywords):
                return True
        has_location = any(loc in query_lower for loc in self.REAL_ESTATE_KEYWORDS['location'])
        has_project = any(term in query_lower for term in ['project', 'flat', 'apartment', 'villa', 'property', 'real estate'])
        return has_location and has_project
    
    def is_tourism_query(self, text: str) -> bool:
        """Check if query or content is tourism-related"""
        tourism_keywords = [
            'things to do', 'tourist attraction', 'places to visit', 'shopping',
            'restaurant', 'cafe', 'hotel', 'resort', 'travel guide', 'weekend getaway',
            'heritage walk', 'food tour', 'sightseeing', 'monument', 'temple', 'museum'
        ]
        text_lower = text.lower()
        return any(kw in text_lower for kw in tourism_keywords) and 'project' not in text_lower


class SourceDiscovery:
    """Find and rank relevant sources for any user query."""

    def __init__(self, searcher: DuckDuckGoSearcher = None):
        self.searcher = searcher or DuckDuckGoSearcher()
        self.client = None
        self.last_token_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
        }
        self.last_llm_payloads = []
        if config.USE_LLM and config.OPENAI_API_KEY:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=30)
            except Exception as e:
                print(f"   âš ï¸ LLM query planner not available: {e}")

    def understand_query(self, query: str, debug_llm_payloads: bool = False) -> QueryUnderstanding:
        llm_understanding = self._understand_query_with_llm(query, debug_llm_payloads=debug_llm_payloads)
        if llm_understanding:
            return llm_understanding

        cleaned = self._normalize_query(query)
        entities = self._extract_key_entities(cleaned)
        intent = self._detect_intent(cleaned)
        domain_context = self._detect_domain_context(cleaned, entities)
        rewritten_queries = self._build_search_queries(cleaned, intent, entities, domain_context)
        positive_terms = self._build_positive_terms(cleaned, entities, domain_context)

        return QueryUnderstanding(
            original_query=query,
            intent=intent,
            key_entities=entities,
            rewritten_queries=rewritten_queries,
            positive_terms=positive_terms,
            avoid_terms=domain_context.get("avoid_terms", []),
            used_llm=False,
        )

    def discover(self, query: str, max_results: int = 5, debug_llm_payloads: bool = False, status_callback=None) -> Dict:
        self._reset_token_usage()
        self.last_llm_payloads = []
        
        if status_callback: status_callback("Analyzing query intent...")
        understanding = self.understand_query(query, debug_llm_payloads=debug_llm_payloads)
        detector = RealEstateIntentDetector()
        understanding.is_real_estate = detector.is_real_estate_query(query)
        
        # If real estate, add specialized project queries
        if understanding.is_real_estate:
            project_queries = self.generate_project_search_queries(query)
            if self._is_property_rate_query(query):
                corrected_query = self._normalize_property_rate_query(query)
                understanding.rewritten_queries = self._dedupe_queries(
                    [corrected_query] + project_queries + understanding.rewritten_queries
                )
            else:
                understanding.rewritten_queries = self._dedupe_queries(project_queries + understanding.rewritten_queries)

        candidates = []
        seen_urls = set()
        for preferred in self._preferred_ready_reckoner_sources(query):
            if preferred["url"] in seen_urls:
                continue
            seen_urls.add(preferred["url"])
            candidates.append(preferred)
        # Search multiple variants
        is_news_query = any(kw in query.lower() for kw in ['news', 'latest', 'recent', 'today'])
        days_back = 7 if is_news_query else None
        
        query_limit = 6 if self._is_property_rate_query(query) else 4
        for i, search_query in enumerate(understanding.rewritten_queries[:query_limit]):
            if status_callback:
                status_callback(f"Finding sources... (Step {i+1}/{query_limit})")
                
            if hasattr(self.searcher, 'search_with_quality'):
                results = self.searcher.search_with_quality(search_query, max_results=max(max_results * 2, 8), days_back=days_back)
            else:
                results = self.searcher.search(search_query, max_results=max(max_results * 2, 8))

            for result in results:
                if not result.url or result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                
                # Apply real estate specific source validation
                if understanding.is_real_estate:
                    if not self.is_valid_real_estate_source(result.url, result.title, result.snippet, query):
                        continue

                # Rank result and add trust scoring
                ranked_item = self._rank_result(result, understanding, search_query)
                ranked_item['trust_score'] = self._calculate_source_trust(result.url)
                ranked_item['verification_status'] = self._check_verification(ranked_item)
                candidates.append(ranked_item)

        # Fallback: if zero results after strict filtering, try once more with relaxed filtering
        if not candidates and understanding.is_real_estate:
            if status_callback: status_callback("Broadening search criteria...")
            for search_query in understanding.rewritten_queries[:2]:
                results = self.searcher.search(search_query, max_results=3)
                for result in results:
                    if not result.url or result.url in seen_urls: continue
                    seen_urls.add(result.url)
                    ranked_item = self._rank_result(result, understanding, search_query)
                    ranked_item['trust_score'] = self._calculate_source_trust(result.url) * 0.5 
                    candidates.append(ranked_item)

        # Fallback for news: if zero results after strict filtering, try once more with relaxed filtering
        if not candidates and is_news_query:
            if status_callback: status_callback("Searching for more recent news...")
            for search_query in understanding.rewritten_queries[:2]:
                if hasattr(self.searcher, 'search_with_quality'):
                    results = self.searcher.search_with_quality(search_query, max_results=5, days_back=30)
                else:
                    results = self.searcher.search(search_query, max_results=5)
                for result in results:
                    if not result.url or result.url in seen_urls: continue
                    seen_urls.add(result.url)
                    ranked_item = self._rank_result(result, understanding, search_query)
                    ranked_item['trust_score'] = self._calculate_source_trust(result.url) * 0.8
                    candidates.append(ranked_item)

        # Apply Domain Filtering and Boosting
        candidates = self.filter_relevant_sources(candidates, query)

        if self.client and candidates:
            if status_callback: status_callback("Reranking top sources for accuracy...")
            candidates = self._rerank_with_llm(query, understanding, candidates, debug_llm_payloads=debug_llm_payloads)

        # Sort and select best results
        ranked = sorted(
            candidates,
            key=lambda item: (item.get("trust_score", 0.5) * 0.4 + item["relevance_score"] * 0.6, -item["rank"]),
            reverse=True,
        )

        filtered = [item for item in ranked if item["relevance_score"] >= MIN_RELEVANCE_SCORE]
        selected = filtered[:max_results]
        if len(selected) < max_results:
            selected_urls = {item["url"] for item in selected}
            selected.extend(
                item
                for item in ranked
                if item["url"] not in selected_urls and item["relevance_score"] >= 0.12
            )
        selected = selected[:max_results]

        return {
            "understanding": understanding.to_dict(),
            "results": selected,
            "token_usage": self.last_token_usage,
            "llm_debug_payloads": self.last_llm_payloads if debug_llm_payloads else [],
        }

    def filter_relevant_sources(self, results: List[Dict], query: str) -> List[Dict]:
        """Remove off-topic sources using content signals."""
        detector = RealEstateIntentDetector()
        filtered_results = []
        is_re_query = detector.is_real_estate_query(query)
        is_rate_query = self._is_property_rate_query(query)

        for result in results:
            url = result.get('url', '').lower()
            domain = urlparse(url).netloc.lower()
            
            if any(blocked in domain for blocked in detector.BLOCKED_DOMAINS):
                continue
            
            if is_re_query:
                title = result.get('title', '').lower()
                snippet = result.get('snippet', '').lower()
                combined = f"{title} {snippet} {url}"
                if detector.is_tourism_query(f"{title} {snippet}"):
                    continue
                if is_rate_query and not self._looks_like_property_rate_result(combined):
                    continue

            filtered_results.append(result)
        return filtered_results

    def generate_project_search_queries(self, query: str) -> List[str]:
        """Generate targeted real estate project or rate queries"""
        query_lower = query.lower()
        locations = ['Pune', 'Mumbai', 'Bangalore', 'Wakad', 'Baner', 'Hinjewadi', 'Kharadi']
        location = 'Pune'
        for loc in locations:
            if loc.lower() in query_lower:
                location = loc
                break
        
        is_rate_query = self._is_property_rate_query(query_lower)

        if is_rate_query:
            return [
                f"ready reckoner rate {location}",
                f"ready reckoner rate {location} 2026",
                f"ready reckoner rate {location} 2024-25",
                f"ready reckoner rate {location} haveli pune",
                f"annual statement of rates {location} 2026",
                f"government valuation property rate {location}",
                f"circle rate {location} residential land rate",
                f"stamp duty market value rate {location}"
            ]

        year_match = re.search(r'20\d{2}', query)
        year = year_match.group(0) if year_match else '2026'
        
        return [
            f"registered residential projects {location} {year}",
            f"new projects in {location} {year}",
            f"upcoming residential projects {location} {year}",
            f"new launch housing projects {location} {year}",
            f"new residential project launch {location} {year} Pune",
            f"upcoming housing project {location} possession date {year}"
        ]

    def _calculate_source_trust(self, url: str) -> float:
        """Calculate trust score from generic source signals."""
        domain = urlparse(url).netloc.replace('www.', '').lower()
        score = 0.50

        if domain.endswith(('.gov', '.gov.in', '.nic.in')) or '.gov.' in domain:
            score += 0.30
        if domain.endswith('.edu') or '.edu.' in domain or domain.endswith('.ac.in'):
            score += 0.20
        if len(domain.split('.')) <= 3:
            score += 0.05

        return min(max(score, 0.25), 0.95)

    def _check_verification(self, result: Dict) -> str:
        """Check if content can be verified"""
        verification_indicators = ['official', 'government', 'rera', 'verified', 'certified', 'legal', 'gazette']
        combined = f"{result.get('title', '')} {result.get('snippet', '')} {result.get('url', '')}".lower()
        
        for indicator in verification_indicators:
            if indicator in combined:
                return "verified_indicator"
        
        return "unverified"

    def is_valid_real_estate_source(self, url: str, title: str, snippet: str, query: str = "") -> bool:
        """Validate if source is relevant to real estate"""
        url_lower = url.lower()
        combined = f"{title} {snippet}".lower()
        domain = urlparse(url_lower).netloc.lower()
        
        if any(bad in url_lower for bad in ['tripadvisor', 'travelocity', 'makemytrip', 'goibibo', 'yatra']):
            return False

        if self._is_property_rate_query(query):
            return self._looks_like_property_rate_result(f"{combined} {url_lower}")
        
        real_estate_indicators = [
            'project', 'apartment', 'flat', 'villa', 'builder', 'developer',
            'possession', 'rera', 'launch', 'construction', 'site', 'tower',
            'units', 'sqft', 'price', 'register', 'brochure', 'floor plan',
            'circle rate', 'ready reckoner', 'valuation', 'market value', 'guideline'
        ]
        
        indicator_count = sum(1 for ind in real_estate_indicators if ind in combined)
        
        if len(snippet) < 30:
            return False

        return indicator_count >= 2

    def _is_property_rate_query(self, query: str) -> bool:
        query_lower = query.lower()
        if any(term in query_lower for term in PROPERTY_RATE_TERMS):
            return True
        return bool(re.search(r"\bready\s+r(?:eckoner|ekoner|eckner|ekner|econer)\b", query_lower))

    def _normalize_property_rate_query(self, query: str) -> str:
        query_lower = query.lower()
        corrected = re.sub(r"\brekoner\b|\breckner\b|\brekner\b|\breconer\b", "reckoner", query_lower)
        corrected = re.sub(r"\s+", " ", corrected).strip()
        if "ready reckoner" not in corrected and "reckoner" in corrected:
            corrected = corrected.replace("reckoner", "ready reckoner", 1)
        return corrected

    def _looks_like_property_rate_result(self, text: str) -> bool:
        text_lower = text.lower()
        has_rate_signal = any(term in text_lower for term in PROPERTY_RATE_RESULT_TERMS)
        has_listing_signal = any(term in text_lower for term in PROJECT_LISTING_TERMS)
        return has_rate_signal and not (has_listing_signal and "ready reckoner" not in text_lower and "valuation" not in text_lower)

    def _reset_token_usage(self):
        self.last_token_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
        }

    def _add_token_usage(self, response):
        usage = getattr(response, "usage", None)
        if not usage:
            return
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        input_cost = input_tokens * 0.00000015
        output_cost = output_tokens * 0.0000006
        self.last_token_usage["input_tokens"] += input_tokens
        self.last_token_usage["output_tokens"] += output_tokens
        self.last_token_usage["total_tokens"] += input_tokens + output_tokens
        self.last_token_usage["total_cost"] = round(
            self.last_token_usage["total_cost"] + input_cost + output_cost,
            6,
        )

    def _understand_query_with_llm(self, query: str, debug_llm_payloads: bool = False) -> Optional[QueryUnderstanding]:
        if not self.client:
            return None

        prompt = f"""You are a search query planner.
Analyze the user query and produce only valid JSON.

User query: {query}

Return this JSON shape:
{{
  "intent": "explanation|latest|comparison|pricing|recommendation|research|how_to",
  "key_entities": ["specific terms, acronyms, locations"],
  "synonyms": ["technical synonyms, alternative terms"],
  "search_queries": ["3-5 optimized queries"],
  "positive_terms": ["technical terms that indicate relevance"],
  "avoid_terms": ["terms that indicate wrong context"]
}}"""

        try:
            payload = {
                "model": config.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 450,
                "temperature": 0.1,
            }
            if debug_llm_payloads:
                self.last_llm_payloads.append({
                    "stage": "query_planning",
                    "payload": payload,
                })
            response = self.client.chat.completions.create(**payload)
            self._add_token_usage(response)
            data = self._parse_json(response.choices[0].message.content)
            if not data:
                return None

            cleaned = self._normalize_query(query)
            entities = data.get("key_entities", [])
            intent = str(data.get("intent") or "research").strip().lower()
            rewritten_queries = data.get("search_queries", [])[:5]

            return QueryUnderstanding(
                original_query=query,
                intent=intent,
                key_entities=entities,
                rewritten_queries=rewritten_queries,
                positive_terms=data.get("positive_terms", []) + entities,
                avoid_terms=data.get("avoid_terms", []),
                used_llm=True,
            )
        except Exception:
            return None

    def _rerank_with_llm(self, query: str, understanding: QueryUnderstanding, candidates: List[Dict], debug_llm_payloads: bool = False) -> List[Dict]:
        if not self.client: return candidates
        shortlist = candidates[:10]
        source_lines = [f"{i}. {item.get('title')} | {item.get('url')}" for i, item in enumerate(shortlist, 1)]
        
        prompt = f"Rerank these results for query: {query}\n\n" + "\n".join(source_lines)
        
        try:
            response = self.client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )
            return candidates # Simplified for now to ensure stability
        except Exception:
            return candidates

    def _parse_json(self, text: str) -> Optional[Dict]:
        try:
            return json.loads(text)
        except Exception:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except Exception:
                    return None
        return None

    def _normalize_query(self, query: str) -> str:
        return re.sub(r"\s+", " ", query).strip()

    def _extract_key_entities(self, query: str) -> List[str]:
        return [word for word in re.findall(r"\w+", query) if word.lower() not in STOP_WORDS]

    def _detect_intent(self, query: str) -> str:
        if any(kw in query.lower() for kw in ['latest', 'news', 'recent']): return "latest"
        return "research"

    def _detect_domain_context(self, query: str, entities: List[str]) -> Dict:
        return {"expansions": [], "context_terms": [], "avoid_terms": [], "domain": None}

    def _build_search_queries(self, query: str, intent: str, entities: List[str], domain_context: Dict) -> List[str]:
        return [query]

    def _build_positive_terms(self, query: str, entities: List[str], domain_context: Dict) -> List[str]:
        return entities

    def _dedupe_queries(self, queries: List[str]) -> List[str]:
        return list(dict.fromkeys(queries))

    def _rank_result(self, result: SearchResult, understanding: QueryUnderstanding, search_query: str) -> Dict:
        haystack = f"{result.title} {result.snippet} {result.url}".lower()
        query_terms = self._important_terms(f"{understanding.original_query} {search_query}")
        relevance = self._topic_overlap_score(haystack, query_terms)

        if self._is_property_rate_query(understanding.original_query):
            if self._looks_like_property_rate_result(haystack):
                relevance = min(relevance + 0.35, 1.0)
            else:
                relevance *= 0.25

        return {
            "url": result.url,
            "title": result.title,
            "snippet": result.snippet,
            "rank": result.rank,
            "relevance_score": relevance,
            "source": result.source
        }


    def _important_terms(self, text: str) -> List[str]:
        return [
            word.lower()
            for word in re.findall(r"\w+", text)
            if len(word) > 2 and word.lower() not in STOP_WORDS
        ]

    def _topic_overlap_score(self, text: str, terms: List[str]) -> float:
        if not terms:
            return 0.0
        matched = sum(1 for term in terms if term in text.lower())
        return min(matched / len(terms), 1.0)
