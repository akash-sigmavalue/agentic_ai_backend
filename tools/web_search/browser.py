from __future__ import annotations

"""
High-Accuracy Content Extraction with Trust Scoring and Multi-strategy fallback.
"""

import re
import json
import time
import requests
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import trafilatura
from readability import Document
from tenacity import retry, stop_after_attempt, wait_exponential

from core.web_search.config import config
from utils.web_search.timestamps import extract_publish_date, get_time_ago
from utils.web_search.validation import ExtractedData


class ContentProcessor:
    """
    Multi-strategy content extractor with confidence scoring and high accuracy.
    """
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        self.max_content_length = config.MAX_CONTENT_LENGTH
    
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
    def fetch_html(self, url: str, timeout: int = 15) -> Optional[str]:
        """Fetch HTML content from URL"""
        try:
            response = self.session.get(url, timeout=timeout)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or 'utf-8'
            return response.text
        except Exception as e:
            print(f"Failed to fetch {url}: {e}")
            return None

    def extract_with_confidence(self, url: str, html: str, query: str = "") -> ExtractedData:
        """
        Extract content with confidence scoring
        """
        # Try multiple extraction methods
        extraction_results = self._try_all_extraction_methods(html, url)
        
        # Score each method's result
        best_result = self._select_best_extraction(extraction_results, query)
        best_result.url = url
        
        # Extract structured data (JSON-LD, Schema.org)
        structured_data = self._extract_structured_data(html)
        if structured_data:
            best_result = self._merge_structured_data(best_result, structured_data)
        
        # Extract key facts, numbers, entities
        best_result.key_facts = self._extract_key_facts(best_result.main_content, query)
        best_result.numbers = self._extract_numbers(best_result.main_content)
        best_result.dates = self._extract_dates(best_result.main_content)
        best_result.locations = self._extract_locations(best_result.main_content)
        best_result.entities = self._extract_entities(best_result.main_content)
        
        # Calculate final confidence
        best_result.confidence_score = self._calculate_confidence(best_result, url, query)
        
        return best_result

    def _try_all_extraction_methods(self, html: str, url: str) -> List[Dict]:
        """Try multiple extraction methods and collect results"""
        results = []
        table_text = self._extract_tables_as_text(html)
        if table_text:
            results.append({
                'method': 'html_tables',
                'content': table_text,
                'length': len(table_text),
                'quality_score': 0.90
            })
        # Method 1: Trafilatura (best for articles)
        try:
            content = trafilatura.extract(html, include_comments=False, include_tables=True)
            if content and len(content) > 200:
                results.append({
                    'method': 'trafilatura',
                    'content': content,
                    'length': len(content),
                    'quality_score': 0.85
                })
        except:
            pass
        
        # Method 2: Readability (Mozilla's algorithm)
        try:
            doc = Document(html)
            content = doc.summary()
            title = doc.title()
            if content and len(content) > 200:
                results.append({
                    'method': 'readability',
                    'content': content,
                    'title': title,
                    'length': len(content),
                    'quality_score': 0.80
                })
        except:
            pass
        
        # Method 3: BeautifulSoup with main content detection
        try:
            soup = BeautifulSoup(html, 'html.parser')
            for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                tag.decompose()
            
            main_selectors = ['main', 'article', '[role="main"]', '.content', '#content', '.post-content']
            main_content = None
            for selector in main_selectors:
                main_content = soup.select_one(selector)
                if main_content:
                    break
            
            content = main_content.get_text(separator=' ', strip=True) if main_content else soup.get_text(separator=' ', strip=True)
            content = re.sub(r'\s+', ' ', content)
            
            if content and len(content) > 200:
                results.append({
                    'method': 'beautifulsoup',
                    'content': content,
                    'length': len(content),
                    'quality_score': 0.75
                })
        except:
            pass
        
        return results

    def _select_best_extraction(self, results: List[Dict], query: str) -> ExtractedData:
        """Select the best extraction result"""
        if not results:
            return self._empty_result("", "No content extracted")
        
        for result in results:
            if query:
                query_words = set(query.lower().split())
                content_words = set(result['content'].lower().split())
                overlap = len(query_words & content_words)
                relevance = overlap / max(len(query_words), 1)
                result['relevance_score'] = relevance
            else:
                result['relevance_score'] = 0.5
            result['final_score'] = result['quality_score'] * 0.6 + result['relevance_score'] * 0.4
        
        best = max(results, key=lambda x: x['final_score'])
        return ExtractedData(
            url="",
            title=best.get('title', ''),
            main_content=best['content'][:10000],
            key_facts=[],
            numbers=[],
            dates=[],
            locations=[],
            entities=[],
            confidence_score=best['final_score'],
            extraction_method=best['method'],
            word_count=len(best['content'].split()),
            has_structured_data=False,
            source_trust=0.0
        )

    def _extract_structured_data(self, html: str) -> Optional[Dict]:
        """Extract JSON-LD and Schema.org data"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            scripts = soup.find_all('script', type='application/ld+json')
            for script in scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, list): data = data[0]
                    if data.get('@type') in ['Article', 'NewsArticle', 'Product', 'RealEstateListing']:
                        return {
                            'headline': data.get('headline', ''),
                            'description': data.get('description', ''),
                            'datePublished': data.get('datePublished', ''),
                            'articleBody': data.get('articleBody', '')
                        }
                except: continue
        except: pass
        return None

    def _merge_structured_data(self, extracted: ExtractedData, structured: Dict) -> ExtractedData:
        if structured.get('articleBody'):
            extracted.main_content = structured['articleBody']
            extracted.extraction_method = "json-ld+" + extracted.extraction_method
            extracted.has_structured_data = True
        return extracted

    def _extract_key_facts(self, content: str, query: str) -> List[str]:
        sentences = re.split(r'[.!?]+', content)
        facts = []
        query_words = set(query.lower().split()) if query else set()
        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 50 or len(sentence) > 300: continue
            score = 30 if re.search(r'\d+', sentence) else 0
            sentence_lower = sentence.lower()
            score += sum(10 for word in query_words if word in sentence_lower)
            if any(ind in sentence_lower for ind in ['price', 'rate', 'cost', 'unit', 'sqft', 'area']):
                score += 15
            if score >= 30: facts.append(sentence)
        return facts[:5]

    def _extract_numbers(self, content: str) -> List[Dict]:
        patterns = [
            (r'â‚¹?\s*(\d+(?:,\d+)*(?:\.\d+)?)\s*(lakh|crore|thousand|million)', 'currency'),
            (r'(\d+(?:,\d+)?)\s*(sq\.?ft|sq\.?m|square feet)', 'area'),
            (r'(\d+)\s*(?:BHK|bhk|bedroom)', 'bhk'),
            (r'(\d+(?:\.\d+)?)%', 'percentage'),
        ]
        numbers = []
        for pattern, type_name in patterns:
            for match in re.findall(pattern, content, re.IGNORECASE):
                val = match[0] if isinstance(match, tuple) else match
                numbers.append({'value': val, 'type': type_name, 'context': self._get_context(content, val)})
        return numbers[:10]

    def _get_context(self, content: str, match_str: str) -> str:
        idx = content.lower().find(match_str.lower())
        if idx >= 0:
            return content[max(0, idx - 50):min(len(content), idx + 100)].strip()
        return ""

    def _extract_dates(self, content: str) -> List[str]:
        patterns = [r'\d{4}-\d{2}-\d{2}', r'\d{2}/\d{2}/\d{4}', r'\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}']
        dates = []
        for p in patterns: dates.extend(re.findall(p, content))
        return list(set(dates))[:5]

    def _extract_locations(self, content: str) -> List[str]:
        locations = ['Pune', 'Mumbai', 'Bangalore', 'Hyderabad', 'Chennai', 'Delhi', 'Wakad', 'Baner', 'Hinjewadi', 'Kharadi']
        return [loc for loc in locations if loc.lower() in content.lower()]

    def _extract_entities(self, content: str) -> List[str]:
        patterns = [r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:project|tower|building|society)', r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:builder|developer|properties)']
        entities = []
        for p in patterns: entities.extend(re.findall(p, content))
        return list(set(entities))[:10]

    TOURISM_KEYWORDS = [
        'tourism', 'tourist', 'things to do', 'shopping', 'dining', 'restaurant',
        'cafe', 'butterfly park', 'trampoline', 'trekking', 'places to visit',
        'attractions', 'entertainment', 'weekend getaway', 'outing'
    ]

    def _is_tourism_content(self, content: str) -> bool:
        """Check if content is about tourism (not real estate)"""
        content_lower = content.lower()
        tourism_count = sum(1 for kw in self.TOURISM_KEYWORDS if kw in content_lower)
        return tourism_count >= 3

    def _is_real_estate_content(self, content: str) -> bool:
        """Check if content is about real estate"""
        real_estate_keywords = [
            'project', 'apartment', 'flat', 'villa', 'builder', 'developer',
            'possession', 'rera', 'launch', 'construction', 'site', 'tower',
            'units', 'configurations', 'bhk', 'sqft', 'price', 'registrations'
        ]
        content_lower = content.lower()
        re_count = sum(1 for kw in real_estate_keywords if kw in content_lower)
        return re_count >= 2

    def _calculate_confidence(self, data: ExtractedData, url: str, query: str) -> float:
        score = {'json-ld': 30, 'trafilatura': 26, 'readability': 24, 'beautifulsoup': 20}.get(data.extraction_method.split('+')[-1], 15)
        source_trust = self._infer_source_trust(data, url, query)
        trust = source_trust * 25
        data.source_trust = source_trust
        score += trust
        score += 20 if data.word_count > 500 else (15 if data.word_count > 200 else 5)
        if data.has_structured_data: score += 10
        score += min(len(data.key_facts) * 3, 15)
        return min(score, 100)

    def _infer_source_trust(self, data: ExtractedData, url: str, query: str) -> float:
        """Estimate source quality from generic signals instead of named websites."""
        domain = urlparse(url).netloc.lower().replace('www.', '')
        path = urlparse(url).path.lower()
        score = 0.5

        if domain.endswith(('.gov', '.gov.in', '.nic.in')) or '.gov.' in domain:
            score += 0.25
        if domain.endswith('.edu') or '.edu.' in domain or domain.endswith('.ac.in'):
            score += 0.15
        if data.has_structured_data:
            score += 0.10
        if data.word_count >= 500:
            score += 0.10
        elif data.word_count >= 200:
            score += 0.05
        if path.endswith('.pdf'):
            score += 0.05
        if query:
            query_words = {word.lower() for word in query.split() if len(word) > 2}
            content = data.main_content.lower()
            if query_words:
                matched = sum(1 for word in query_words if word in content)
                score += min((matched / len(query_words)) * 0.15, 0.15)
        return min(max(score, 0.25), 1.0)

    def process_batch(self, urls: List[str], query: str = "", delay: float = 1.0, status_callback=None) -> List[Dict]:
        results = []
        for i, url in enumerate(urls):
            if status_callback:
                status_callback(f"Reading source {i + 1}/{len(urls)}...")
            print(f"  [{i+1}/{len(urls)}] Processing: {url[:60]}...")
            html = self.fetch_html(url)
            if html:
                extracted = self.extract_with_confidence(url, html, query)
                pub_date = extract_publish_date(html, url)
                results.append({
                    'url': url,
                    'title': extracted.title or "No Title",
                    'content': extracted.main_content,
                    'published_date': pub_date,
                    'time_ago': get_time_ago(pub_date) if pub_date else "Recently",
                    'confidence_score': extracted.confidence_score,
                    'source_trust': extracted.source_trust,
                    'extracted_data': extracted  # Keep the full object for validation
                })
            if i < len(urls) - 1: time.sleep(delay)
        return results

    def _empty_result(self, url: str, error: str) -> ExtractedData:
        return ExtractedData(url=url, title="", main_content="", key_facts=[], numbers=[], dates=[], locations=[], entities=[], confidence_score=0, extraction_method="none", word_count=0, has_structured_data=False, source_trust=0)
