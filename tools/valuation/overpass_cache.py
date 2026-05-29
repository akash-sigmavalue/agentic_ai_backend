import os
import sqlite3
import hashlib
import json
import logging
from typing import Optional, Dict

logger = logging.getLogger("overpass_cache")
CACHE_DB_PATH = os.path.join(os.path.dirname(__file__), "overpass_cache.sqlite")

def get_cached_overpass_request(query: str) -> Optional[Dict]:
    """
    Retrieves cached Overpass query result if exists.
    """
    # Normalize query by stripping whitespace to ensure consistent hashing
    normalized = " ".join(query.strip().split())
    query_hash = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    try:
        conn = sqlite3.connect(CACHE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS overpass_cache (hash TEXT PRIMARY KEY, query TEXT, response TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        cursor.execute("SELECT response FROM overpass_cache WHERE hash = ?", (query_hash,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.error(f"Failed to read from overpass cache: {e}")
    return None

def set_cached_overpass_request(query: str, response: Dict):
    """
    Stores Overpass query result in local SQLite database.
    """
    normalized = " ".join(query.strip().split())
    query_hash = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    try:
        conn = sqlite3.connect(CACHE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS overpass_cache (hash TEXT PRIMARY KEY, query TEXT, response TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        cursor.execute(
            "INSERT OR REPLACE INTO overpass_cache (hash, query, response) VALUES (?, ?, ?)",
            (query_hash, normalized, json.dumps(response))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to write to overpass cache: {e}")
