from __future__ import annotations

"""
Automatic search-result quality detection.

The scoring here is deterministic and adaptive:
- deterministic: source quality is calculated from structural/content/query signals
- adaptive: domain reputation is learned from previously encountered results

It intentionally avoids hardcoded allow/deny domain lists. Existing domain-specific
safety rules in callers can still run before or after this layer.
"""

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core.web_search.config import config


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for",
    "from", "give", "have", "how", "i", "in", "is", "it", "me", "most",
    "no", "number", "of", "on", "or", "regarding", "search", "show", "that",
    "the", "this", "to", "want", "what", "whatever", "when", "where", "which",
    "will", "with", "you",
}

def _detect_keyword_stuffing(text: str) -> bool:
    words = re.findall(r"\b\w{4,}\b", text.lower())
    if len(words) < 80:
        return False
    word_counts = Counter(words)
    return any(count > max(15, len(words) * 0.08) for count in word_counts.values())


def _important_terms(text: str) -> List[str]:
    return [
        word.lower()
        for word in re.findall(r"[A-Za-z0-9]+", text or "")
        if len(word) > 2 and word.lower() not in STOP_WORDS
    ]


class QualitySignalDetector:
    """Detect source quality from result text without domain allow/deny lists."""

    POSITIVE_SIGNALS = {
        "has_code_block": {"weight": 20, "detect": lambda t: bool(re.search(r"```\w*\n.*?```", t, re.DOTALL))},
        "has_inline_code": {"weight": 6, "detect": lambda t: bool(re.search(r"`[^`]+`", t))},
        "has_import_statement": {"weight": 10, "detect": lambda t: bool(re.search(r"^\s*(import|from)\s+\w+", t, re.MULTILINE))},
        "has_function_def": {"weight": 10, "detect": lambda t: bool(re.search(r"\bdef\s+\w+\s*\(", t))},
        "has_error_handling": {"weight": 6, "detect": lambda t: bool(re.search(r"\btry\s*:|except\s+\w+:", t))},
        "has_assertion": {"weight": 5, "detect": lambda t: bool(re.search(r"\bassert\b", t))},
        "has_pytest": {"weight": 10, "detect": lambda t: bool(re.search(r"\bpytest\b|def\s+test_\w+|pytest\.raises", t, re.I))},
        "has_example_output": {"weight": 6, "detect": lambda t: bool(re.search(r"Output:|>>>|\.\.\.", t))},
        "has_scraping_library": {"weight": 8, "detect": lambda t: bool(re.search(r"\b(scrapy|requests|beautifulsoup|bs4|selenium|playwright)\b", t, re.I))},
        "has_fuzzy_matching_library": {"weight": 10, "detect": lambda t: bool(re.search(r"\b(rapidfuzz|thefuzz|fuzzywuzzy|difflib|SequenceMatcher|fuzz\.ratio)\b", t, re.I))},
        "has_structured_table": {"weight": 7, "detect": lambda t: bool(re.search(r"\|.*\|", t))},
        "has_ordered_list": {"weight": 5, "detect": lambda t: bool(re.search(r"^\d+\.\s+\w+", t, re.MULTILINE))},
        "has_specific_numbers": {"weight": 8, "detect": lambda t: bool(re.search(r"\b\d+(?:[.,]\d+)?(?:%|/-| sq\.?ft| sqm| lakh| crore)?\b", t, re.I))},
        "has_currency_or_rate": {"weight": 8, "detect": lambda t: bool(re.search(r"(?:rs\.?|inr|₹|\$)\s*\d|rate|fee|cost|price|valuation", t, re.I))},
        "has_date_or_year": {"weight": 6, "detect": lambda t: bool(re.search(r"\b(?:19|20)\d{2}(?:-\d{2})?\b|\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b", t))},
        "has_legal_or_official_terms": {"weight": 8, "detect": lambda t: bool(re.search(r"\b(act|rule|section|notification|circular|gazette|official|government|department|authority|compliance)\b", t, re.I))},
        "has_property_rate_terms": {"weight": 8, "detect": lambda t: bool(re.search(r"ready reckoner|circle rate|guideline value|market value|stamp duty|survey no|cts|gat no|asr", t, re.I))},
    }

    NEGATIVE_SIGNALS = {
        "excessive_keyword_repetition": {"weight": -20, "detect": _detect_keyword_stuffing},
        "too_many_links": {"weight": -12, "detect": lambda t: t.count("http") > 50},
        "marketing_language": {"weight": -14, "detect": lambda t: bool(re.search(r"best\s+seo|rank\s+higher|get\s+more\s+traffic|contact\s+us\s+for|limited\s+time\s+offer", t, re.I))},
        "very_short_content": {"weight": -10, "detect": lambda t: len(t.strip()) < 160},
        "generic_placeholder_text": {"weight": -15, "detect": lambda t: bool(re.search(r"lorem\s+ipsum|placeholder|dummy\s+text", t, re.I))},
        "too_many_ads_indicators": {"weight": -10, "detect": lambda t: t.lower().count("sponsored") > 2 or t.lower().count("advertisement") > 1},
        "login_or_wall_text": {"weight": -8, "detect": lambda t: bool(re.search(r"\blog\s*in\b|sign\s*up|subscribe to continue|enable javascript", t, re.I))},
    }

    @classmethod
    def calculate_quality_score(
        cls,
        title: str,
        snippet: str,
        content: str = "",
        query: str = "",
    ) -> Dict[str, Any]:
        combined = f"{title} {snippet} {content}"

        positive_score = 35
        negative_score = 0
        matched_positives: List[Tuple[str, int]] = [("base_content_quality", 35)]
        matched_negatives: List[Tuple[str, int]] = []

        for signal_name, signal_data in cls.POSITIVE_SIGNALS.items():
            if signal_data["detect"](combined):
                weight = int(signal_data["weight"])
                positive_score += weight
                matched_positives.append((signal_name, weight))

        for signal_name, signal_data in cls.NEGATIVE_SIGNALS.items():
            if signal_data["detect"](combined):
                weight = int(signal_data["weight"])
                negative_score += weight
                matched_negatives.append((signal_name, weight))

        query_terms = _important_terms(query)
        combined_lower = combined.lower()
        if query_terms:
            matched_terms = [term for term in query_terms if term in combined_lower]
            overlap = len(matched_terms) / max(len(query_terms), 1)
            overlap_weight = round(overlap * 25)
            positive_score += overlap_weight
            matched_positives.append((f"query_term_overlap:{len(matched_terms)}/{len(query_terms)}", overlap_weight))
            if overlap == 0:
                negative_score -= 25
                matched_negatives.append(("no_query_term_overlap", -25))
            elif overlap < 0.25:
                negative_score -= 12
                matched_negatives.append(("low_query_term_overlap", -12))

            normalized_query = re.sub(r"\s+", " ", query.lower()).strip()
            if normalized_query and normalized_query in combined_lower:
                positive_score += 10
                matched_positives.append(("exact_query_phrase", 10))

        total_score = max(0, min(100, positive_score + negative_score))
        return {
            "quality_score": total_score,
            "positive_signals": matched_positives,
            "negative_signals": matched_negatives,
            "positive_score": positive_score,
            "negative_score": negative_score,
            "is_high_quality": total_score >= 50,
        }


class DomainReputationLearner:
    """Learns domain reputation from encountered results and persists it locally."""

    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = Path(storage_path or Path(config.CACHE_DIR) / "domain_reputation.json")
        self.domain_scores: Dict[str, float] = {}
        self.domain_encounters: Dict[str, int] = {}
        self._load()

    def record_result(self, url: str, quality_score: float) -> None:
        domain = self._extract_domain(url)
        if not domain:
            return

        score = max(0.0, min(100.0, float(quality_score)))
        encounters = self.domain_encounters.get(domain, 0)
        current_score = self.domain_scores.get(domain, score)
        if encounters == 0:
            new_score = score
        else:
            # Exponential moving average keeps the learner adaptive to recent quality.
            new_score = (current_score * 0.80) + (score * 0.20)

        self.domain_scores[domain] = round(new_score, 2)
        self.domain_encounters[domain] = encounters + 1
        self._save()

    def get_domain_boost(self, url: str) -> float:
        domain = self._extract_domain(url)
        if not domain or domain not in self.domain_scores:
            return 1.0

        score = self.domain_scores[domain]
        multiplier = 0.5 + (score / 100)
        return max(0.5, min(1.5, multiplier))

    def get_top_domains(self, limit: int = 10) -> List[Tuple[str, float]]:
        sorted_domains = sorted(self.domain_scores.items(), key=lambda item: item[1], reverse=True)
        return sorted_domains[:limit]

    def _extract_domain(self, url: str) -> str:
        try:
            domain = urlparse(url or "").netloc.lower()
            domain = re.sub(r"^www\.", "", domain)
            return domain.split(":")[0]
        except Exception:
            return ""

    def _load(self) -> None:
        try:
            if not self.storage_path.exists():
                return
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            self.domain_scores = {str(k): float(v) for k, v in data.get("scores", {}).items()}
            self.domain_encounters = {str(k): int(v) for k, v in data.get("encounters", {}).items()}
        except Exception:
            self.domain_scores = {}
            self.domain_encounters = {}

    def _save(self) -> None:
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": int(time.time()),
                "scores": self.domain_scores,
                "encounters": self.domain_encounters,
            }
            self.storage_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            pass


def filter_search_result_automatic(
    url: str,
    title: str,
    snippet: str,
    content: str = "",
    query: str = "",
    reputation_learner: DomainReputationLearner = None,
    min_score: float = 45.0,
) -> Dict[str, Any]:
    quality = QualitySignalDetector.calculate_quality_score(title, snippet, content, query=query)
    base_score = float(quality["quality_score"])

    if reputation_learner:
        boost = reputation_learner.get_domain_boost(url)
        final_score = min(100.0, base_score * boost)
    else:
        boost = 1.0
        final_score = base_score

    code_blocks = []
    if content:
        code_blocks = re.findall(r"```(\w*)\n(.*?)```", content, re.DOTALL)
    if not code_blocks and snippet:
        code_blocks = re.findall(r"`([^`]+)`", snippet)

    is_accepted = final_score >= min_score and quality["positive_score"] >= 35
    return {
        "url": url,
        "title": title,
        "quality_score": round(final_score, 2),
        "base_score": round(base_score, 2),
        "domain_boost": round(boost, 3),
        "is_accepted": is_accepted,
        "has_code": len(code_blocks) > 0,
        "code_blocks": code_blocks[:3],
        "positive_signals": quality["positive_signals"],
        "negative_signals": quality["negative_signals"],
        "extracted_libraries": extract_libraries(f"{title} {snippet} {content}"),
        "rejection_reason": None if is_accepted else f"Quality score {final_score:.0f}/100 below threshold or insufficient positive signals",
    }


def extract_libraries(text: str) -> List[str]:
    libraries = set()
    for match in re.findall(r"(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_\.]*)", text or ""):
        libraries.add(match.split(".")[0])

    common_libs = [
        "scrapy", "requests", "beautifulsoup", "bs4", "selenium", "playwright",
        "hashlib", "json", "csv", "pandas", "numpy", "trafilatura", "pytest",
        "rapidfuzz", "thefuzz", "difflib",
    ]
    for lib in common_libs:
        if re.search(rf"\b{re.escape(lib)}\b", text or "", re.I):
            libraries.add("beautifulsoup" if lib == "bs4" else lib)

    return sorted(libraries)[:8]
