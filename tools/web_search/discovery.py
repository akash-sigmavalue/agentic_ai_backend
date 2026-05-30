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
    "no", "number", "of", "on", "or", "regarding", "search", "show", "that", "the", "this",
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
    "ready reckon",
    "reckoner rate",
    "reckon rate",
    "ready rekoner",
    "rekoner rate",
    "ready reckner",
    "ready recknor",
    "recknor rate",
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

DEVELOPMENT_REGULATION_TERMS = [
    "fsi", "far", "floor space index", "floor area ratio", "development control",
    "development control regulation", "dcr", "dc rules", "dcp rules", "dcpr",
    "udcpr", "building bye laws", "building bylaws", "building rules",
    "land development", "land development rules", "land development regulations",
    "layout rules", "subdivision rules", "plot development", "plot layout",
    "zoning", "land use", "setback", "setbacks", "building height",
    "permissible height", "premium fsi", "fungible fsi", "tdr", "tod",
    "road width", "plot coverage", "municipal rules", "planning authority",
    "development plan", "master plan", "town planning",
]

DEVELOPMENT_REGULATION_SOURCE_TERMS = [
    "official", "government", "municipal", "municipality", "municipal corporation",
    "planning authority", "urban development", "town planning", "gazette",
    "notification", "circular", "development control", "dcr", "dcpr", "udcpr",
    "building bye laws", "building bylaws", "land development", "layout",
    "subdivision", "development plan", "master plan",
]

DEVELOPMENT_REGULATION_AVOID_TERMS = [
    "calculator", "blog", "consultant", "builder", "real estate portal",
    "property for sale", "flats for sale", "apartment for sale", "youtube",
]

PROJECT_LISTING_TERMS = [
    "new projects",
    "residential projects",
    "upcoming projects",
    "project in",
    "launch project",
    "launched projects",
    "flats",
    "apartments",
    "bhk",
    "possession",
    "amenities",
    "floor plan",
    "builder",
    "developer",
    "nobroker",
    "housing.com",
    "magicbricks",
    "99acres",
    "propertypistol",
    "homebazaar",
]

PROPERTY_RATE_AVOID_TERMS = [
    "new launch",
    "upcoming project",
    "residential project",
    "property for sale",
    "rent",
    "rental",
    "roi",
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
            'market value', 'property valuation', 'government valuation',
            'fsi', 'far', 'development control', 'dcr', 'dcpr', 'udcpr',
            'zoning', 'setback', 'building height', 'land use'
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
        if any(term in query_lower for term in DEVELOPMENT_REGULATION_TERMS):
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
        understanding.rewritten_queries = self._dedupe_queries(
            self._build_specific_search_queries(query, understanding) + understanding.rewritten_queries
        )

        is_development_regulation_query = self._is_development_regulation_query(query)
        if is_development_regulation_query:
            understanding.intent = "development_regulation"
            understanding.positive_terms = self._dedupe_queries(
                understanding.positive_terms + [
                    "official", "government", "municipal", "planning authority",
                    "gazette", "notification", "development control", "building rules",
                ]
            )
            understanding.avoid_terms = self._dedupe_queries(
                understanding.avoid_terms + DEVELOPMENT_REGULATION_AVOID_TERMS
            )
            understanding.rewritten_queries = self._dedupe_queries(
                self.generate_development_regulation_queries(query) + understanding.rewritten_queries
            )
        
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
        if self._is_property_rate_query(query):
            for result in self._direct_property_rate_sources(query):
                if result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                ranked_item = self._rank_result(result, understanding, result.title)
                ranked_item['trust_score'] = max(self._calculate_source_trust(result.url), 0.75)
                ranked_item['verification_status'] = self._check_verification(ranked_item)
                candidates.append(ranked_item)

        # Search multiple variants
        is_news_query = any(kw in query.lower() for kw in ['news', 'latest', 'recent', 'today'])
        days_back = 7 if is_news_query else None
        
        query_limit = 8 if is_development_regulation_query else (6 if self._is_property_rate_query(query) else 5)
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
                if is_development_regulation_query:
                    ranked_item['trust_score'] = max(
                        ranked_item['trust_score'],
                        self._development_regulation_source_trust(result.url, result.title, result.snippet),
                    )
                ranked_item['verification_status'] = self._check_verification(ranked_item)
                candidates.append(ranked_item)

        # Fallback: if zero results after strict filtering, try once more with relaxed filtering
        if not candidates and understanding.is_real_estate and not self._is_property_rate_query(query):
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
        is_development_regulation_query = self._is_development_regulation_query(query)

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
                if is_development_regulation_query and not self._looks_like_development_regulation_result(combined):
                    continue

            filtered_results.append(result)
        return filtered_results

    def generate_development_regulation_queries(self, query: str) -> List[str]:
        """Generate official-source queries for FSI/DCR/zoning/building-rule questions."""
        location = self._extract_development_regulation_location(query)
        normalized = self._normalize_query(query)
        queries = [
            f"{normalized} official",
            f"{normalized} government notification",
            f"{normalized} municipal corporation",
            f"{normalized} planning authority",
            f"{normalized} gazette PDF",
            f"development control regulations {location} FSI FAR land development official PDF",
            f"land development rules layout subdivision plot development {location} municipal official",
            f"building rules zoning setbacks height land use {location} municipal official",
            f"town planning development plan {location} land development control regulation",
        ]
        if re.search(r"\bmaharashtra\b|\bpune\b|\bmumbai\b|\bthane\b|\bnashik\b|\bnagpur\b", normalized, re.I):
            queries.extend([
                f"Unified Development Control and Promotion Regulations {location} FSI official",
                f"UDCPR {location} FSI premium TDR TOD road width official PDF",
            ])
        return self._dedupe_queries(queries)

    def generate_project_search_queries(self, query: str) -> List[str]:
        """Generate targeted real estate project or rate queries"""
        query_lower = query.lower()
        location = self._extract_real_estate_location(query)
        
        is_rate_query = self._is_property_rate_query(query_lower)

        if is_rate_query:
            survey_number = self._extract_survey_number(query)
            queries = []
            if survey_number:
                queries.extend([
                    f"ready reckoner rate {location} survey no {survey_number}",
                    f"{location} survey no {survey_number} ready reckoner rate",
                    f"annual statement of rates {location} survey no {survey_number}",
                    f"government valuation {location} survey no {survey_number}",
                ])
            queries.extend([
                f"ready reckoner rate {location}",
                f"ready reckoner rate {location} 2026",
                f"ready reckoner rate {location} 2024-25",
                f"ready reckoner rate {location} 2024",
                f"ready reckoner rate {location} haveli pune",
                f"annual statement of rates {location} 2026",
                f"government valuation property rate {location}",
                f"circle rate {location} residential land rate",
                f"stamp duty market value rate {location}"
            ])
            return self._dedupe_queries(queries)

        year_match = re.search(r'20\d{2}', query)
        year = year_match.group(0) if year_match else '2026'

        if self._is_existing_real_estate_query(query):
            view_terms = []
            if any(term in query_lower for term in ["sea view", "waterfront", "lake view", "lake-view", "river view", "water view"]):
                view_terms = ["waterfront", "lake view", "water view"]
            descriptor = " ".join(view_terms) if view_terms else "existing"
            return self._dedupe_queries([
                f"{descriptor} real estate projects {location}",
                f"{descriptor} homes for sale {location}",
                f"{descriptor} condos for sale {location}",
                f"{descriptor} apartments {location}",
                f"existing real estate projects {location}",
                f"residential communities {location}",
            ])
        
        return [
            f"registered residential projects {location} {year}",
            f"new projects in {location} {year}",
            f"upcoming residential projects {location} {year}",
            f"new launch housing projects {location} {year}",
            f"new residential project launch {location} {year} Pune",
            f"upcoming housing project {location} possession date {year}"
        ]

    def _direct_property_rate_sources(self, query: str) -> List[SearchResult]:
        """Add known public ready-reckoner/eASR sources for locality-rate queries."""
        location = self._extract_property_rate_location(query) or self._extract_real_estate_location(query)
        location_key = location.lower().strip()
        location_map = {
            "baner": ("pune", "haveli", "baner"),
            "banner": ("pune", "haveli", "baner"),
            "balewadi": ("pune", "haveli", "balewadi"),
            "aundh": ("pune", "haveli", "aundh"),
            "wakad": ("pune", "haveli", "wakad"),
            "kharadi": ("pune", "haveli", "kharadi"),
            "hadapsar": ("pune", "haveli", "hadapsar"),
            "kothrud": ("pune", "haveli", "kothrud"),
            "hinjewadi": ("pune", "mulshi", "hinjewadi"),
        }
        if location_key not in location_map:
            return []

        current_year = time.localtime().tm_year
        years = re.findall(r"\b20\d{2}\b", query) or [str(current_year), str(current_year - 1), str(current_year - 2)]
        district, taluka, village = location_map[location_key]
        results = []

        for year in self._dedupe_queries(years + [str(current_year)]):
            results.append(SearchResult(
                url=f"https://www.e-stampdutyreadyreckoner.com/reckoner/{year}/{district}/{taluka}/{village}",
                title=f"Ready Reckoner Rate {village.title()} {year}",
                snippet=f"Ready reckoner / Annual Statement of Rates page for {village.title()}, {taluka.title()}, {district.title()}.",
                source="direct-ready-reckoner",
                rank=len(results) + 1,
            ))
            results.append(SearchResult(
                url=f"https://www.onlinereadyreckoner.com/reckoner-{district}/{year}/{taluka}/{village}",
                title=f"Online Ready Reckoner {village.title()} {year}",
                snippet=f"Ready reckoner reference for {village.title()}, {taluka.title()}, {district.title()} for {year}.",
                source="direct-ready-reckoner",
                rank=len(results) + 1,
            ))

        results.extend([
            SearchResult(
                url=f"https://findcirclerate.com/india/maharashtra/{district}/{village}",
                title=f"Ready Reckoner {village.title()} {district.title()}",
                snippet=f"Circle-rate / ready-reckoner calculator reference for {village.title()}, {district.title()}.",
                source="direct-ready-reckoner",
                rank=len(results) + 1,
            ),
            SearchResult(
                url=f"https://igreval.maharashtra.gov.in/eASR2.0/eASRCommon.aspx?hDistName={district.title()}",
                title=f"IGR Maharashtra eASR Rates {district.title()}",
                snippet=f"Official Maharashtra IGR eASR entry point for Annual Statement of Rates in {district.title()}.",
                source="direct-ready-reckoner",
                rank=len(results) + 2,
            ),
            SearchResult(
                url=f"https://easr.igrmaharashtra.gov.in/eASRCommon.aspx?hDistName={district.title()}",
                title=f"Maharashtra eASR Rates {district.title()}",
                snippet=f"Official Maharashtra eASR entry point for ready-reckoner / market value rates in {district.title()}.",
                source="direct-ready-reckoner",
                rank=len(results) + 3,
            ),
        ])
        return results

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
        verification_indicators = [
            'official', 'government', 'rera', 'verified', 'certified', 'legal',
            'gazette', 'municipal corporation', 'planning authority',
            'urban development', 'town planning', 'notification', 'circular',
        ]
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

        if self._is_development_regulation_query(query):
            return self._looks_like_development_regulation_result(f"{combined} {url_lower}")
        
        real_estate_indicators = [
            'project', 'apartment', 'flat', 'villa', 'builder', 'developer',
            'possession', 'rera', 'launch', 'construction', 'site', 'tower',
            'units', 'sqft', 'price', 'register', 'brochure', 'floor plan',
            'circle rate', 'ready reckoner', 'valuation', 'market value', 'guideline',
            'fsi', 'far', 'development control', 'dcr', 'dcpr', 'udcpr',
            'zoning', 'setback', 'building height', 'land use'
        ]
        
        indicator_count = sum(1 for ind in real_estate_indicators if ind in combined)
        
        if len(snippet) < 30:
            return False

        return indicator_count >= 2

    def _is_property_rate_query(self, query: str) -> bool:
        query_lower = query.lower()
        if any(term in query_lower for term in PROPERTY_RATE_TERMS):
            return True
        return bool(re.search(r"\bready\s+r(?:eckoner|eckon|ecknor|ekoner|eckner|ekner|econer)\b", query_lower))

    def _is_development_regulation_query(self, query: str) -> bool:
        query_lower = query.lower()
        if any(term in query_lower for term in DEVELOPMENT_REGULATION_TERMS):
            return True
        return bool(re.search(r"\b(?:fsi|far|dcr|dcpr|udcpr|tdr|tod)\b", query_lower))

    def _normalize_property_rate_query(self, query: str) -> str:
        query_lower = query.lower()
        corrected = re.sub(r"\breckon\b|\brekoner\b|\brecknor\b|\breckner\b|\brekner\b|\breconer\b", "reckoner", query_lower)
        corrected = re.sub(r"\bbanner\b", "baner", corrected)
        corrected = re.sub(r"\s+", " ", corrected).strip()
        if "ready reckoner" not in corrected and "reckoner" in corrected:
            corrected = corrected.replace("reckoner", "ready reckoner", 1)
        return corrected

    def _extract_survey_number(self, query: str) -> str:
        match = re.search(
            r"\b(?:survey|survay|srv|s\.?\s*no|gat|plot|cts)\s*(?:no\.?|number|#|is|:|-)?\s*([A-Za-z0-9][A-Za-z0-9/-]*)",
            query,
            re.IGNORECASE,
        )
        if match:
            return match.group(1).strip(" .,#:-")
        return ""

    def _is_existing_real_estate_query(self, query: str) -> bool:
        query_lower = query.lower()
        existing_terms = [
            "existing", "available", "for sale", "resale", "ready to move",
            "ready-to-move", "sea view", "waterfront", "lake view", "lake-view",
            "river view", "water view", "listing", "listings",
        ]
        return any(term in query_lower for term in existing_terms)

    def _extract_real_estate_location(self, query: str) -> str:
        query_lower = query.lower()
        if re.search(r"\bbanner\b", query_lower):
            return "Baner"
        locations = ['Pune', 'Mumbai', 'Bangalore', 'Wakad', 'Baner', 'Hinjewadi', 'Kharadi']
        for loc in locations:
            if loc.lower() in query_lower:
                return loc

        match = re.search(r"\b(?:in|at|near|around)\s+([A-Za-z][A-Za-z\s,.-]{2,80})", query)
        if match:
            location = re.split(r"\b(?:with|for|and|that|which|having)\b", match.group(1), maxsplit=1)[0]
            location = re.sub(r"\s+", " ", location).strip(" ,.-")
            if location:
                return location.title()

        return "requested location"

    def _extract_property_rate_location(self, query: str) -> str:
        query_lower = query.lower()
        if re.search(r"\bbanner\b", query_lower):
            return "Baner"
        for location in sorted(KNOWN_REAL_ESTATE_LOCATIONS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(location)}\b", query_lower):
                return location.title()

        match = re.search(r"\b(?:for|in|at|of)\s+([a-z][a-z\s-]{2,40})", query_lower)
        if not match:
            return ""

        words = [
            word
            for word in re.findall(r"[a-z]+", match.group(1))
            if word not in STOP_WORDS and word not in {"survey", "survay", "no", "number", "rate"}
        ]
        return " ".join(words[:3]).title()

    def _extract_development_regulation_location(self, query: str) -> str:
        query_lower = query.lower()
        for location in sorted(KNOWN_REAL_ESTATE_LOCATIONS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(location)}\b", query_lower):
                return location.title()

        match = re.search(r"\b(?:for|in|at|of|near|around)\s+([a-z][a-z\s,.-]{2,80})", query_lower)
        if match:
            location = re.split(
                r"\b(?:fsi|far|dcr|dcpr|udcpr|rules?|regulations?|zoning|setbacks?|height|land use|for|with|and)\b",
                match.group(1),
                maxsplit=1,
            )[0]
            words = [
                word
                for word in re.findall(r"[a-z]+", location)
                if word not in STOP_WORDS
            ]
            if words:
                return " ".join(words[:4]).title()

        return "requested location"

    def _looks_like_property_rate_result(self, text: str) -> bool:
        text_lower = text.lower()
        if any(term in text_lower for term in PROPERTY_RATE_AVOID_TERMS):
            return False
        has_rate_signal = any(term in text_lower for term in PROPERTY_RATE_RESULT_TERMS)
        has_property_signal = any(term in text_lower for term in [
            "ready reckoner", "reckoner", "circle rate", "guideline value",
            "government valuation", "market value", "annual statement of rates",
            "asr", "stamp duty", "valuation", "survey no", "survey number",
            "cts", "gat no", "igr", "easr",
        ])
        has_listing_signal = any(term in text_lower for term in PROJECT_LISTING_TERMS)
        return has_rate_signal and has_property_signal and not (
            has_listing_signal and "ready reckoner" not in text_lower and "valuation" not in text_lower
        )

    def _looks_like_development_regulation_result(self, text: str) -> bool:
        text_lower = text.lower()
        if any(term in text_lower for term in DEVELOPMENT_REGULATION_AVOID_TERMS):
            official_signal = any(term in text_lower for term in ["gov", "nic.in", "municipal", "authority", "gazette"])
            if not official_signal:
                return False
        has_rule_signal = any(term in text_lower for term in DEVELOPMENT_REGULATION_TERMS)
        has_source_signal = any(term in text_lower for term in DEVELOPMENT_REGULATION_SOURCE_TERMS)
        return has_rule_signal or has_source_signal

    def _development_regulation_source_trust(self, url: str, title: str = "", snippet: str = "") -> float:
        domain = urlparse(url or "").netloc.replace("www.", "").lower()
        combined = f"{title} {snippet} {url}".lower()
        score = self._calculate_source_trust(url or "")

        if domain.endswith((".gov.in", ".nic.in")) or ".gov." in domain:
            score = max(score, 0.90)
        if any(term in combined for term in ["municipal corporation", "planning authority", "urban development", "town planning"]):
            score = max(score, 0.82)
        if any(term in combined for term in ["gazette", "notification", "circular", "official", "pdf"]):
            score = max(score, 0.78)
        if any(term in combined for term in DEVELOPMENT_REGULATION_AVOID_TERMS):
            score = min(score, 0.55)

        return min(max(score, 0.25), 0.95)

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

        prompt = f"""You are an expert web-search query planner.
Analyze the user query and produce only valid JSON.

Your job is to make source discovery work for any domain without hardcoded rules.
Infer the domain, the user's exact information need, and the best source types.
For legal, regulatory, government, planning, policy, tax, court, or compliance questions:
- Prefer official government/authority PDFs, notifications, acts/rules, circulars, FAQs, and regulator pages.
- Expand acronyms and include the official full form when useful.
- Create queries that target applicability, exclusions, exemptions, exceptions, jurisdiction, and authority names when the user asks where something applies or does not apply.
- Avoid dictionary pages, generic explainers, forums, and low-evidence SEO pages unless no primary source exists.

User query: {query}

Return this JSON shape:
{{
  "intent": "explanation|latest|comparison|pricing|recommendation|research|how_to|legal_regulatory|applicability",
  "key_entities": ["specific terms, acronyms, locations"],
  "synonyms": ["technical synonyms, alternative terms"],
  "search_queries": ["5-8 optimized queries, with official-source and PDF-focused variants where useful"],
  "positive_terms": ["technical terms that indicate relevance"],
  "avoid_terms": ["terms that indicate wrong context"],
  "preferred_source_types": ["official authority", "government PDF", "regulator FAQ", "primary law"]
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
            rewritten_queries = data.get("search_queries", [])[:8]

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
        shortlist = candidates[: min(len(candidates), 20)]
        source_lines = [
            (
                f"{i}. Title: {item.get('title')}\n"
                f"   URL: {item.get('url')}\n"
                f"   Snippet: {item.get('snippet')}\n"
                f"   Search query: {item.get('search_query') or ''}"
            )
            for i, item in enumerate(shortlist, 1)
        ]

        prompt = f"""You are an evidence-source reranker for a web-search agent.
Score each result for whether it can help answer the user's exact query.

User query: {query}
Detected intent: {understanding.intent}
Key entities: {', '.join(understanding.key_entities or [])}

Rules:
- Give high scores to primary/official sources, authority PDFs, laws/rules, notifications, regulator pages, and pages whose title/snippet directly mention the requested entity and constraint.
- Give low scores to dictionary pages, generic definitions, unrelated domains, SEO pages, pages about a different topic, and pages that only share a location word.
- For applicability/exclusion questions, prefer sources that mention applicability, exceptions, exclusions, jurisdiction, authority, regulation scope, or official rules.
- Do not guess content that is not visible in the title/snippet/url.

Results:
{chr(10).join(source_lines)}

Return only JSON:
{{
  "ranked": [
    {{
      "index": 1,
      "relevance": 0.0,
      "officialness": 0.0,
      "answers_query": true,
      "reason": "short reason"
    }}
  ]
}}"""
        
        try:
            payload = {
                "model": config.LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1200,
                "temperature": 0.0,
            }
            if debug_llm_payloads:
                self.last_llm_payloads.append({
                    "stage": "source_reranking",
                    "payload": payload,
                })
            response = self.client.chat.completions.create(**payload)
            self._add_token_usage(response)
            data = self._parse_json(response.choices[0].message.content)
            ranked_items = data.get("ranked", []) if isinstance(data, dict) else []
            scored_by_index = {}
            for item in ranked_items:
                try:
                    idx = int(item.get("index")) - 1
                except Exception:
                    continue
                if 0 <= idx < len(shortlist):
                    scored_by_index[idx] = item

            if not scored_by_index:
                return candidates

            reranked = []
            for idx, candidate in enumerate(shortlist):
                score = scored_by_index.get(idx)
                if not score:
                    continue
                relevance = self._coerce_score(score.get("relevance"))
                officialness = self._coerce_score(score.get("officialness"))
                answers_query = bool(score.get("answers_query", relevance >= 0.5))
                updated = dict(candidate)
                updated["llm_relevance_score"] = relevance
                updated["llm_officialness_score"] = officialness
                updated["llm_relevance_reason"] = str(score.get("reason") or "")
                updated["relevance_score"] = max(
                    candidate.get("relevance_score", 0.0) * 0.45,
                    (relevance * 0.75) + (officialness * 0.25),
                )
                updated["trust_score"] = max(candidate.get("trust_score", 0.5), officialness)
                if answers_query or relevance >= 0.35:
                    reranked.append(updated)

            if not reranked:
                return candidates

            reranked.sort(
                key=lambda item: (
                    item.get("llm_relevance_score", 0.0) * 0.7
                    + item.get("llm_officialness_score", 0.0) * 0.3,
                    item.get("trust_score", 0.5),
                ),
                reverse=True,
            )

            seen = {item.get("url") for item in reranked}
            tail = [item for item in candidates if item.get("url") not in seen]
            return reranked + tail
        except Exception:
            return candidates

    def _coerce_score(self, value) -> float:
        try:
            score = float(value)
        except Exception:
            return 0.0
        if score > 1.0:
            score = score / 100.0
        return min(max(score, 0.0), 1.0)

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
        if self._is_development_regulation_query(query):
            return "development_regulation"
        if any(kw in query.lower() for kw in ['latest', 'news', 'recent']): return "latest"
        return "research"

    def _detect_domain_context(self, query: str, entities: List[str]) -> Dict:
        return {"expansions": [], "context_terms": [], "avoid_terms": [], "domain": None}

    def _build_search_queries(self, query: str, intent: str, entities: List[str], domain_context: Dict) -> List[str]:
        return self._dedupe_queries([query] + self._build_constraint_queries(query, entities))

    def _build_positive_terms(self, query: str, entities: List[str], domain_context: Dict) -> List[str]:
        return entities

    def _dedupe_queries(self, queries: List[str]) -> List[str]:
        cleaned = []
        seen = set()
        for query in queries:
            normalized = re.sub(r"\s+", " ", str(query or "")).strip()
            key = normalized.lower()
            if normalized and key not in seen:
                seen.add(key)
                cleaned.append(normalized)
        return cleaned

    def _build_specific_search_queries(self, query: str, understanding: QueryUnderstanding) -> List[str]:
        queries = [query]
        entities = understanding.key_entities or self._extract_key_entities(query)
        queries.extend(self._build_constraint_queries(query, entities))
        return self._dedupe_queries(queries)

    def _build_constraint_queries(self, query: str, entities: List[str]) -> List[str]:
        constraints = self._extract_query_constraints(query, entities)
        focus_terms = constraints["phrases"] + constraints["ids"] + constraints["years"]
        if not focus_terms:
            return []

        normalized = self._normalize_query(query)
        quoted_focus = " ".join(f'"{term}"' for term in focus_terms[:4])
        core_entities = " ".join(constraints["entities"][:4])
        queries = [
            f"{normalized} {quoted_focus}".strip(),
            " ".join(part for part in [core_entities, quoted_focus] if part),
            " ".join(part for part in [core_entities, quoted_focus, "official"] if part),
        ]

        if constraints["years"]:
            latest_year = constraints["years"][-1]
            queries.append(" ".join(part for part in [core_entities, quoted_focus, latest_year] if part))

        return queries

    def _extract_query_constraints(self, query: str, entities: List[str]) -> Dict[str, List[str]]:
        quoted_phrases = re.findall(r'"([^"]{2,80})"', query)
        named_phrases = re.findall(r"\b[A-Z][A-Za-z0-9]*(?:\s+[A-Z0-9][A-Za-z0-9]*){1,5}\b", query)
        years = re.findall(r"\b(?:19|20)\d{2}(?:-\d{2})?\b", query)
        identifiers = re.findall(
            r"\b(?:no\.?|number|id|code|section|rule|article|survey|plot|cts|case|order|form|model|version)\s*(?:is|:|#|-)?\s*([A-Za-z0-9][A-Za-z0-9./_-]{0,40})",
            query,
            re.IGNORECASE,
        )
        slash_ids = re.findall(r"\b[A-Za-z]{1,8}[-/]?\d{1,8}(?:[-/][A-Za-z0-9]{1,12})*\b", query)
        spec_values = re.findall(r"\b\d+(?:\.\d+)?\s*(?:gb|tb|mb|bhk|sqft|sq\.?ft|sq\.?m|km|m|%|percent|lakh|crore)\b", query, re.IGNORECASE)
        meaningful_entities = [
            entity
            for entity in entities
            if len(entity) > 2 and entity.lower() not in STOP_WORDS
        ]

        return {
            "phrases": self._dedupe_queries(quoted_phrases + named_phrases),
            "years": self._dedupe_queries(years),
            "ids": self._filter_constraint_values(identifiers + slash_ids + spec_values),
            "entities": self._dedupe_queries(meaningful_entities),
        }

    def _filter_constraint_values(self, values: List[str]) -> List[str]:
        blocked = STOP_WORDS | {"no.", "number.", "id.", "code.", "section.", "rule."}
        return self._dedupe_queries(
            value.strip(" .,#:-")
            for value in values
            if value and value.strip(" .,#:-").lower() not in blocked
        )

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
