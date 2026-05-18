import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # DuckDuckGo settings
    DDG_MAX_RESULTS = int(os.getenv("DDG_MAX_RESULTS", 10))
    DDG_TIMEOUT = int(os.getenv("DDG_TIMEOUT", 10))
    DDG_REGION = os.getenv("DDG_REGION", "wt-wt")  # wt-wt = worldwide
    
    # LLM Settings (OpenAI - optional, token-efficient)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    USE_LLM = bool(OPENAI_API_KEY)
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")  # Cheaper, good for analysis
    MAX_TOKENS = int(os.getenv("MAX_TOKENS", 500))
    
    # Content processing
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 5000))
    EXTRACT_READABLE = os.getenv("EXTRACT_READABLE", "true").lower() == "true"

    # Optional deep crawling and document extraction
    ENABLE_CRAWLING = os.getenv("ENABLE_CRAWLING", "true").lower() == "true"
    CRAWL_MAX_DEPTH = int(os.getenv("CRAWL_MAX_DEPTH", 2))
    CRAWL_MAX_PAGES = int(os.getenv("CRAWL_MAX_PAGES", 25))
    CRAWL_TOP_RESULTS = int(os.getenv("CRAWL_TOP_RESULTS", 3))
    CRAWL_TIMEOUT = int(os.getenv("CRAWL_TIMEOUT", 10))
    USE_LLM_GUIDED_CRAWL = os.getenv("USE_LLM_GUIDED_CRAWL", "false").lower() == "true"
    ENABLE_DOCUMENT_DOWNLOAD = os.getenv("ENABLE_DOCUMENT_DOWNLOAD", "true").lower() == "true"
    DOCUMENT_DOWNLOAD_DIR = os.getenv("DOCUMENT_DOWNLOAD_DIR", "data/web_documents")
    DOCUMENT_MAX_DOWNLOADS = int(os.getenv("DOCUMENT_MAX_DOWNLOADS", 8))
    DOCUMENT_MAX_BYTES = int(os.getenv("DOCUMENT_MAX_BYTES", 15 * 1024 * 1024))
    
    # Cache settings
    CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
    CACHE_TTL = int(os.getenv("CACHE_TTL", 1800))  # 30 minutes in seconds
    CACHE_DIR = os.getenv("CACHE_DIR", "data/cache")
    
    # Rate limiting
    REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", 1.0))
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
    
    # Response format
    DEFAULT_FORMAT = os.getenv("DEFAULT_FORMAT", "markdown")  # markdown, json, text
    MAX_RESULTS_IN_RESPONSE = int(os.getenv("MAX_RESULTS_IN_RESPONSE", 10))
    
    # Web interface (optional)
    API_HOST = os.getenv("API_HOST", "0.0.0.0")
    API_PORT = int(os.getenv("API_PORT", 8000))

config = Config()
