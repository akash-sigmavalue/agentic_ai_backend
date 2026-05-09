"""
Timestamp extraction and formatting utilities.
Extracts publication dates from HTML and formats them as relative time strings.
"""

import re
from datetime import datetime, timezone
from typing import Optional


def extract_publish_date(html: str, url: str) -> Optional[str]:
    """
    Extract publication date from HTML using multiple strategies.
    Returns an ISO-format date string or None.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')

    # Strategy 1: Open Graph / meta tags (most reliable)
    meta_properties = [
        'article:published_time',
        'og:article:published_time',
        'article:modified_time',
        'og:updated_time',
    ]
    for prop in meta_properties:
        meta = soup.find('meta', property=prop)
        if meta and meta.get('content'):
            return meta['content']

    meta_names = [
        'date',
        'pubdate',
        'publish_date',
        'published_date',
        'article_date',
        'sailthru.date',
        'DC.date.issued',
        'dcterms.date',
        'last-modified',
    ]
    for name in meta_names:
        meta = soup.find('meta', attrs={'name': re.compile(name, re.I)})
        if meta and meta.get('content'):
            return meta['content']

    # Strategy 2: <time> element with datetime attribute
    time_tag = soup.find('time', attrs={'datetime': True})
    if time_tag:
        return time_tag['datetime']

    # Strategy 3: JSON-LD structured data
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            import json
            data = json.loads(script.string or '')
            if isinstance(data, list):
                data = data[0] if data else {}
            for key in ('datePublished', 'dateModified', 'dateCreated'):
                if key in data:
                    return data[key]
        except Exception:
            pass

    # Strategy 4: Regex patterns in raw HTML (fallback)
    date_patterns = [
        r'published["\']?\s*:\s*["\']([\d\-:T +]+)',
        r'datePublished["\']?\s*:\s*["\']([\d\-:T +]+)',
        r'datetime=["\']([\d\-:T +]+)',
        r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})',
        r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})',
        r'(\d{4}-\d{2}-\d{2})',
    ]

    for pattern in date_patterns:
        match = re.search(pattern, html[:10000], re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return None


def parse_date(date_str: str) -> Optional[datetime]:
    """Try to parse a date string into a datetime object."""
    if not date_str:
        return None

    # Clean up the date string
    date_str = date_str.strip()

    formats = [
        '%Y-%m-%dT%H:%M:%S%z',
        '%Y-%m-%dT%H:%M:%S.%f%z',
        '%Y-%m-%dT%H:%M:%SZ',
        '%Y-%m-%dT%H:%M:%S.%fZ',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d',
        '%d %b %Y',
        '%d %B %Y',
        '%B %d, %Y',
        '%b %d, %Y',
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt
        except ValueError:
            continue

    # Last resort: try fromisoformat
    try:
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except Exception:
        pass

    return None


def get_time_ago(date_str: str) -> str:
    """Convert date string to a human-readable relative time like '3 hours ago'."""
    dt = parse_date(date_str)
    if not dt:
        return "Date unknown"

    # Make both timezone-naive for comparison
    now = datetime.now()
    if dt.tzinfo is not None:
        now = datetime.now(timezone.utc)

    try:
        diff = now - dt
    except TypeError:
        # Mixed tz-aware and tz-naive
        dt = dt.replace(tzinfo=None)
        now = datetime.now()
        diff = now - dt

    if diff.total_seconds() < 0:
        return "Just now"

    seconds = int(diff.total_seconds())

    if seconds < 60:
        return "Just now"
    elif seconds < 3600:
        mins = seconds // 60
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif seconds < 604800:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''} ago"
    elif seconds < 2592000:
        weeks = seconds // 604800
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    elif seconds < 31536000:
        months = seconds // 2592000
        return f"{months} month{'s' if months != 1 else ''} ago"
    else:
        years = seconds // 31536000
        return f"{years} year{'s' if years != 1 else ''} ago"
