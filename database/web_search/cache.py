"""
Caching system - reduces duplicate searches
Token savings: 90%+ for repeated queries
"""

import json
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional
import hashlib
from diskcache import Cache

from core.web_search.config import config


class SearchCache:
    """
    Persistent cache for search results
    Reduces API calls and LLM usage significantly
    """
    
    def __init__(self):
        self.cache_dir = Path(config.CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = config.CACHE_TTL
        
        # Use diskcache for persistent storage
        self.cache = Cache(str(self.cache_dir))
    
    def _get_key(self, query: str, search_type: str = "web") -> str:
        """Generate cache key"""
        key_string = f"{search_type}:{query.lower().strip()}"
        return hashlib.md5(key_string.encode()).hexdigest()
    
    def get(self, query: str, search_type: str = "web") -> Optional[Dict]:
        """Get cached result"""
        key = self._get_key(query, search_type)
        
        if key in self.cache:
            cached = self.cache[key]
            # Check if cache is still valid
            if time.time() - cached.get('timestamp', 0) < self.ttl:
                print(f"Cache hit for: {query}")
                return cached.get('data')
        
        return None
    
    def set(self, query: str, data: Dict, search_type: str = "web"):
        """Cache result"""
        key = self._get_key(query, search_type)
        
        self.cache[key] = {
            'timestamp': time.time(),
            'data': data,
            'query': query,
            'type': search_type
        }
        
        print(f"Cached result for: {query}")
    
    def clear(self, older_than: int = None):
        """Clear old cache entries"""
        if older_than:
            cutoff = time.time() - older_than
            for key in list(self.cache.keys()):
                entry = self.cache.get(key)
                if entry and entry.get('timestamp', 0) < cutoff:
                    del self.cache[key]
        else:
            self.cache.clear()
        
        print("Cache cleared")
    
    def stats(self) -> Dict:
        """Get cache statistics"""
        total = len(self.cache)
        return {
            'total_entries': total,
            'cache_dir': str(self.cache_dir),
            'ttl_seconds': self.ttl,
            'ttl_hours': self.ttl / 3600
        }


# Example usage
if __name__ == "__main__":
    cache = SearchCache()
    print(f"Cache stats: {cache.stats()}")
