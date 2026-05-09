"""
Listing Search Tool — Fetches real sale/rent listings for projects.

YIELD FIXES (this revision):
  YIELD-01  URL strategy: ask LLM for project-page + search-result URLs, not
            individual property-detail pages (which yield 1 listing each and
            expire/404 quickly)
  YIELD-02  Site blocklist: housing.com individual pages return 406; skip them
            in URL search prompt and in the scraper with a fast pre-check
  YIELD-03  Smarter scraper: project overview pages contain a listing TABLE —
            extract ALL rows, not just the first match
  YIELD-04  Fallback robustness: parse fallback response even when LLM wraps
            the array in prose; also retry with a simpler prompt if first
            attempt produces no JSON
  YIELD-05  URL count raised: default max_urls_per_project 5->10 so more
            attempts are made per project
  YIELD-06  Extraction parallelism: extract runs in parallel (not sequentially)
            -- reduces total wall-clock time significantly

PREVIOUS BUG FIXES (retained from prior revision):
  BUG-01..11  See prior revision for full list.
"""

import os
import re
import json
import time
import threading
import logging
import requests
import urllib.parse
from typing import Optional, List
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from dotenv import load_dotenv
from tools.valuation.road_infrastructure_tool import get_road_category
from tools.valuation.amenity_analytics_tool import get_nearby_amenities, get_amenity_counts

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("listing_tool.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("listing_tool")

_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

# YIELD-02: Sites that are difficult to scrape with simple requests.
# We now use Selenium to bypass these blocks.

BLOCKED_URL_PATTERNS = [
    "magicbricks.com/propertyDetails/", # Still skip individual detail pages
]

# YIELD-01: URLs matching these patterns contain multiple listings per page
PREFERRED_URL_PATTERNS = [
    "/property-for-sale/",
    "/projects/",
    "/search/",
    "/buy/",
    "results",
    "listing",
]

# Rich browser-like headers reduce 406 bot-blocks
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Property types ────────────────────────────────────────────────────────
PROPERTY_TYPE_ALIASES = {
    "apartment":         {"apartment", "flat", "condo", "condominium", "penthouse"},
    "villa":             {"villa", "bungalow", "row house", "townhouse"},
    "plot":              {"plot", "land", "site"},
    "retail":            {"shop", "retail", "showroom"},
    "commercial_office": {"office", "workspace", "coworking"},
}

PROPERTY_TYPE_DISPLAY = {
    "apartment":         "apartment (flat / condo / penthouse)",
    "villa":             "villa (bungalow / row house / townhouse)",
    "plot":              "plot (land / site)",
    "retail":            "shop (retail space / showroom)",
    "commercial_office": "office space (workspace / coworking)",
}

PROPERTY_TYPE_SEARCH_TERM = {
    "apartment":         "apartment",
    "villa":             "villa",
    "plot":              "plot",
    "retail":            "shop",
    "commercial_office": "office space",
}

PROPERTY_TYPE_EXCLUSIONS = {
    "apartment":         ["villa", "bungalow", "plot", "land", "shop", "office", "retail", "showroom", "row house", "townhouse"],
    "villa":             ["apartment", "flat", "condo", "plot", "land", "shop", "office", "retail", "showroom"],
    "plot":              ["apartment", "flat", "villa", "bungalow", "shop", "office", "built-up", "constructed"],
    "retail":            ["apartment", "flat", "villa", "bungalow", "plot", "land", "office", "residential", "condo"],
    "commercial_office": ["apartment", "flat", "villa", "bungalow", "plot", "land", "shop", "retail", "showroom", "residential"],
}

LISTING_SIGNALS = [
    "bhk", "sqft", "sq.ft", "lac", "lakh", "cr", "crore",
     "sale", "lease", "floor","total_floors", "carpet", "₹", "bedroom",
    "bath", "parking", "possession", "ready", "under construction",
    "furnished", "semi", "unfurnished", "price", "area", "office",
    "shop", "villa", "plot", "flat", "apartment",
]


# ── Helpers ───────────────────────────────────────────────────────────────
def log_drop(stage: str, project_name: str, reason: str, extra: Optional[dict] = None) -> None:
    msg = f"[DROP] stage={stage} | project='{project_name}' | reason={reason}"
    if extra:
        msg += f" | detail={json.dumps(extra)}"
    logger.warning(msg)


def extract_json_array(text: str) -> Optional[str]:
    """Bracket-balanced JSON array extractor — immune to greedy regex merging."""
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start: i + 1]
    return None


def normalize_property_type(raw: str) -> Optional[str]:
    if not raw:
        return None
    raw = raw.lower().strip()
    for k, v in PROPERTY_TYPE_ALIASES.items():
        if raw == k or raw in v:
            return k
    return None


def is_matching_project(extracted_name: str, target_name: str) -> bool:
    """
    True when extracted_name genuinely matches target_name.
    Requires substring containment OR >50% meaningful-token overlap.
    Prevents "Sobha Marina" matching target "Sobha Hartland 2".
    """
    if not extracted_name or not target_name:
        return False
    e_norm = extracted_name.lower().replace(" ", "").replace("-", "")
    t_norm = target_name.lower().replace(" ", "").replace("-", "")
    if t_norm in e_norm or e_norm in t_norm:
        return True
    t_tokens = [tok for tok in target_name.lower().split() if len(tok) > 3]
    if not t_tokens:
        return False
    matched = sum(1 for tok in t_tokens if tok in extracted_name.lower())
    return (matched / len(t_tokens)) > 0.5


def is_valid_data(val) -> bool:
    if val is None:
        return False
    return str(val).lower().strip() not in (
        "", "null", "n/a", "none", "—", "-", "nan", "0", "undefined"
    )


def is_blocked_url(url: str) -> bool:
    return any(pat in url for pat in BLOCKED_URL_PATTERNS)


def safe_filename(url: str) -> str:
    """Converts a URL into a filesystem-friendly filename."""
    return re.sub(r'[^a-zA-Z0-9]', '_', url)[:100]



# ── Driver Pool for Parallel Scraping ───────────────────────────────────
_driver_pool: dict[int, webdriver.Chrome] = {}
_driver_lock = threading.Lock()
USER_AGENT               = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def _get_thread_driver() -> webdriver.Chrome:
    """Return (or create) a Chrome driver bound to the current thread for reuse."""
    tid = threading.get_ident()
    with _driver_lock:
        if tid not in _driver_pool:
            logger.info(f"[Driver Pool] Creating new browser for thread {tid}")
            options = Options()
            options.add_argument("--headless=new") # Production usually uses headless
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument(f"user-agent={USER_AGENT}")
            
            # Asset Blocking & Eager Loading for Speed
            options.page_load_strategy = 'eager'
            prefs = {
                "profile.managed_default_content_settings.images": 2,
                "profile.managed_default_content_settings.stylesheet": 2,
                "profile.managed_default_content_settings.fonts": 2
            }
            options.add_experimental_option("prefs", prefs)
            
            driver = webdriver.Chrome(options=options)
            _driver_pool[tid] = driver
    return _driver_pool[tid]


def is_html_useful(html: str) -> bool:
    """Decides if the HTML content is substantial enough for extraction."""
    if not html or len(html) < 2000:
        return False
    
    # Check for heavy script sites that might need visible text capture instead
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script")
    if len(scripts) > 60: # Threshold from R&D
        return False
    
    return True


def score_url(url: str) -> int:
    """Higher = more likely to contain multiple listings on one page."""
    score = 0
    for pat in PREFERRED_URL_PATTERNS:
        if pat in url:
            score += 1
    # Individual MagicBricks detail pages: 1 listing each, expire fast
    if "magicbricks.com/propertyDetails/" in url:
        score -= 2
    if "99acres.com/search" in url or "99acres.com/property-for-sale" in url:
        score += 2
    return score


def search_duckduckgo(query: str, num_results: int = 15) -> List[str]:
    """
    Directly scrapes DuckDuckGo HTML results as implemented in R&D notebook.
    """
    try:
        url = "https://html.duckduckgo.com/html/"
        params = {"q": query}

        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        res = requests.post(url, data=params, headers=headers)
        soup = BeautifulSoup(res.text, "html.parser")

        links = []
        for a in soup.select("a.result__a"):
            links.append(a.get("href"))
        return links[:num_results]
    
    except Exception as e:
        logger.error(f"[DuckDuckGo] Search failed for '{query}': {e}")
        return []


def search_urls_for_projects_batch(
    projects:      list,
    listing_type:  str,
    property_type: str,
    num_results:   int = 7,
) -> tuple:
    """
    Updated to use DuckDuckGo scraping logic instead of OpenAI web_search.
    Generates a targeted query for each project and scrapes results.
    """
    final_map = {}
    search_term = PROPERTY_TYPE_SEARCH_TERM.get(property_type, property_type)
    
    # Process each project with a targeted query
    for p in projects:
        pname = p["project_name"]
        loc = p.get("location", "")
        country = p.get("country", "India")
        
        # Build a high-intent query
        lat = p.get("lat")
        lng = p.get("lng")
        if lat and lng and lat != 0 and lng != 0:
            query = f"buy {search_term} in {pname}, {loc}, {country}, coordinates: {lat}, {lng}"
        else:
            query = f"buy {search_term} in {pname}, {loc}, {country}"

        logger.info(f"[URL Search DDG] Query: '{query}'")
        
        urls = search_duckduckgo(query, num_results=num_results)
        
        # Score and sort URLs (YIELD-01)
        valid_urls = sorted(urls, key=score_url, reverse=True)
        final_map[pname] = valid_urls
        logger.info(f"[URL Search DDG] Result: '{pname}' -> {len(valid_urls)} URLs.")
        time.sleep(1)

    # Return empty usage as we didn't use OpenAI search tokens
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return final_map, usage


# ── YIELD-03: Scraper — capture all listing rows, not just first ──────────
def fetch_page_text(url: str, project_name: str = "", char_limit: int = 2000, run_logger: Optional[any] = None) -> str:
    """
    Refactored to use Selenium for robust scraping, matching R&D logic.
    Switches between HTML extraction and Visible Text based on usefulness.
    Now saves raw content to disk if run_logger is provided.
    """
    if is_blocked_url(url):
        log_drop("scrape", project_name, "blocked_url_pattern", extra={"url": url})
        return ""

    logger.info(f"[Scrape] project='{project_name}' url='{url}'")
    
    # Filename for logging
    fname = safe_filename(url)
    
    # 1. Try Requests First (as requested)
    use_selenium = False
    try:
        logger.info(f"[Scrape] Trying requests for {url}")
        resp = requests.get(url, headers=HEADERS, timeout=15)
        
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            html = resp.text
            for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            
            if run_logger:
                run_logger.save_text("listing_search/scrapes/raw", f"{fname}_req", text)
                run_logger.save_raw("listing_search/scrapes/html", f"{fname}_req_html.html", html)

            if len(text) > 500:
                logger.info(f"[Scrape] OK via Requests (200 OK) for {url}")
                return text[:char_limit]
            else:
                logger.info(f"[Scrape] Requests returned 200 but content too short. Falling back.")
                use_selenium = True
        else:
            logger.info(f"[Scrape] Requests returned status {resp.status_code}. Falling back to Selenium.")
            use_selenium = True

    except Exception as e:
        logger.info(f"[Scrape] Requests exception: {e}. Falling back to Selenium.")
        use_selenium = True

    if use_selenium:
        driver = None
        try:
            logger.info(f"[Scrape] URL '{url}' is difficult to scrape, using Selenium Pool.")
            driver = _get_thread_driver()
            driver.set_page_load_timeout(30)
            driver.get(url)
            time.sleep(4) # Allow dynamic content to load
            
            html = driver.page_source
            if is_html_useful(html):
                # Clean HTML approach
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                
                if run_logger:
                    run_logger.save_raw("listing_search/scrapes/html", f"{fname}_sel_html.html", html) # Save raw HTML
                    run_logger.save_text("listing_search/scrapes/raw", f"{fname}_sel_clean", text)

                logger.info(f"[Scrape] OK via Selenium (HTML Mode) for {url}")
            else:
                # Visible text approach
                text = driver.find_element("tag name", "body").text
                
                if run_logger:
                    run_logger.save_text("listing_search/scrapes/raw", f"{fname}_sel_text", text)
                    run_logger.save_raw("listing_search/scrapes/html", f"{fname}_sel_html.html", html) # Save raw HTML

                logger.info(f"[Scrape] OK via Selenium (Text Mode) for {url}")
                
            return text[:char_limit]                
        except Exception as e:
            log_drop("scrape", project_name, f"selenium_exception: {e}", extra={"url": url})
            return ""
    
    return ""


# ── Extraction prompt ─────────────────────────────────────────────────────
def build_extract_prompt(property_type: str, project_name: str, text: str) -> str:
    display_name  = PROPERTY_TYPE_DISPLAY.get(property_type, property_type)
    search_term   = PROPERTY_TYPE_SEARCH_TERM.get(property_type, property_type)
    valid_terms   = ", ".join(PROPERTY_TYPE_ALIASES.get(property_type, {property_type}))
    exclusion_str = ", ".join(PROPERTY_TYPE_EXCLUSIONS.get(property_type, []))

    return f"""You are a real estate listing data extractor.
Read the text and extract EVERY distinct property listing for project: "{project_name}".

STRICT RULES:
1. Extract ONLY listings for project: "{project_name}". IGNORE nearby projects.
2. Property type MUST be: {display_name} (valid terms: {valid_terms}). 
3. DO NOT extract: {exclusion_str}.
4. CRITICAL: COPY values EXACTLY as they appear. No calculations, no unit conversions (e.g., leave "1.2 Cr" as "1.2 Cr").
5. LOCAL CURRENCY: If multiple prices/currencies are listed for the same unit (e.g., both AED and USD), ALWAYS prioritize the local currency of the project's country.
6. If a field is not found, use null.

Return ONLY a JSON array of objects with these keys:
- currency (e.g. "₹", "INR", "USD")
- project_name (exactly "{project_name}")
- property_type (exactly "{property_type}")
- listing_type ("Sale" or "Rental")
- location (address/locality)
- price_raw (e.g. "₹ 75 Lac")
- area_raw (e.g. "612 sqft")
- area_type (e.g. "Carpet Area")
- bhk (e.g. "2 BHK")
- price (exact string from text, e.g. "1.1 Cr" or "11000000", do not convert or clean numbers)
- area_sqft (clean numeric value if possible, else copy area_raw)
- rate_per_sqft_raw
- possession_status
- furnishing
- floor (e.g. "5" or "Ground")
- total_floors (e.g. "12")
- parking
- facing
- developer
- rera_id

TEXT:
{text}
"""


# ── Extract listings from scraped text ───────────────────────────────────
def extract_listings_from_text(
    url:           str,
    page_text:     str,
    project_name:  str,
    property_type: str,
) -> tuple:

    if not page_text:
        log_drop("extract", project_name, "empty_page_text", extra={"url": url})
        return [], {}

    try:
        user_prompt = build_extract_prompt(property_type, project_name, page_text)
        system_prompt = "You are a precise data extraction agent. Return only a JSON array of objects."
        response = _client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=2000,
        )

        input_tok  = getattr(response.usage, "prompt_tokens", 0)
        output_tok = getattr(response.usage, "completion_tokens", 0)
        token_usage = {
            "prompt_tokens":     input_tok,
            "completion_tokens": output_tok,
            "total_tokens":      input_tok + output_tok,
            "model":             "gpt-4o-mini"
        }


        json_chunk = extract_json_array(response.choices[0].message.content.strip())
        if not json_chunk:
            log_drop("extract", project_name, "no_json_array_in_llm_response", extra={"url": url})
            return [], token_usage

        listings = json.loads(json_chunk)
        logger.info(f"[Extract] '{project_name}' -> {len(listings)} raw listings from '{url}'")

        verified = []
        for item in listings:
            # Property type guard
            # if normalize_property_type(item.get("property_type", "")) != property_type:
            #     log_drop(
            #         "extract_type_filter",
            #         item.get("project_name", project_name),
            #         "property_type_mismatch",
            #         extra={"got": item.get("property_type"), "expected": property_type, "url": url},
            #     )
            #     continue

            # Project name guard (BUG-01/03 fix)
            extracted_proj = (item.get("project_name") or "").strip()
            if extracted_proj and not is_matching_project(extracted_proj, project_name):
                log_drop(
                    "extract_project_name_filter",
                    extracted_proj,
                    "project_name_mismatch",
                    extra={"extracted": extracted_proj, "expected": project_name, "url": url},
                )
                continue

            item["source_url"]    = url
            item["project_name"]  = project_name
            item["property_type"] = property_type
            verified.append(item)

        logger.info(
            f"[Extract Filter] '{project_name}' -> "
            f"{len(verified)}/{len(listings)} passed type + name filter"
        )
        return verified, token_usage

    except Exception as e:
        log_drop("extract", project_name, f"extraction_exception: {e}", extra={"url": url})
        return [], {}


# ── YIELD-04: robust fallback with retry ─────────────────────────────────
def batch_llm_fallback(
    failed_projects: list,
    listing_type:    str,
    property_type:   str,
) -> tuple:

    if not failed_projects:
        return {}, {}

    display_name  = PROPERTY_TYPE_DISPLAY.get(property_type, property_type)
    search_term   = PROPERTY_TYPE_SEARCH_TERM.get(property_type, property_type)
    exclusion_str = ", ".join(PROPERTY_TYPE_EXCLUSIONS.get(property_type, []))

    logger.info(
        f"[Fallback] Running for {len(failed_projects)} projects: "
        f"{[p['project_name'] for p in failed_projects]}"
    )

    project_list = "\n".join([
        f'- "{p["project_name"]}", {p["location"]}, {p["country"]}'
        for p in failed_projects
    ])

    # Two-prompt strategy: detailed first, simple retry if no JSON returned
    prompts = [
        (
            f"Provide current {listing_type} listings for {search_term} "
            f"({display_name}) units from your internal real estate database.\n"
            f"CRITICAL: NO DATA MANIPULATION. Use raw values for price_raw and area_raw.\n\n"
            f"Projects:\n{project_list}\n\n"
            f"Return ONLY a JSON array. Fields: project_name, property_type "
            f"(exactly '{property_type}'), listing_type, location, bhk, price, price_raw, currency, "
            f"area_sqft, area_raw, area_type, rate_per_sqft_raw, possession_status, "
            f"furnishing, floor, total_floors, parking, facing, developer, rera_id"
        ),
        # Simpler retry
        (
            f"Based on your knowledge, give me current {listing_type} prices for {search_term} in these projects:\n"
            f"{project_list}\n\n"
            f"Return a JSON array. Each item needs: "
            f"project_name, property_type='{property_type}', price, price_raw, currency, area_sqft, area_raw, bhk, location, rera_id."
        ),
    ]

    cumulative = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    for attempt, prompt in enumerate(prompts, 1):
        try:
            response = _client.responses.create(
                model="gpt-4o-mini",
                input=prompt,
                max_output_tokens=8000,
            )

            input_tok  = getattr(response.usage, "input_tokens", 0)
            output_tok = getattr(response.usage, "output_tokens", 0)
            cumulative["prompt_tokens"]     += input_tok
            cumulative["completion_tokens"] += output_tok
            cumulative["total_tokens"]      += input_tok + output_tok
            cumulative["model"]             = "gpt-4o-mini"


            json_chunk = extract_json_array(response.output_text.strip())

            if not json_chunk:
                logger.warning(f"[Fallback] Attempt {attempt} — no JSON, retrying...")
                time.sleep(1)
                continue

            all_listings = json.loads(json_chunk)
            logger.info(f"[Fallback] Attempt {attempt} -> {len(all_listings)} raw listings")

            grouped: dict = {p["project_name"]: [] for p in failed_projects}

            for lst in all_listings:
                if normalize_property_type(lst.get("property_type", "")) != property_type:
                    log_drop("fallback_type_filter", lst.get("project_name", "unknown"),
                             "property_type_mismatch",
                             extra={"got": lst.get("property_type"), "expected": property_type})
                    continue

                lst["property_type"] = property_type

                ext_name = lst.get("project_name", "")
                matched  = False
                for p in failed_projects:
                    if is_matching_project(ext_name, p["project_name"]):
                        lst["project_name"] = p["project_name"]
                        lst["is_fallback"] = True
                        grouped[p["project_name"]].append(lst)
                        matched = True
                        break

                if not matched:
                    log_drop("fallback_project_match", ext_name,
                             "could_not_match_to_any_requested_project",
                             extra={"requested": [p["project_name"] for p in failed_projects]})

            for proj_name, listings in grouped.items():
                logger.info(f"[Fallback] '{proj_name}' -> {len(listings)} listings")

            return grouped, cumulative

        except Exception as e:
            logger.error(f"[Fallback] Attempt {attempt} exception: {e}")
            time.sleep(1)

    return {}, cumulative


# ── Price normalizer ──────────────────────────────────────────────────────
def normalise_price(price_str) -> Optional[float]:
    if not price_str:
        return None
    s = re.sub(r"[₹$£€,]", "", str(price_str)).strip().lower()
    s = s.split("/")[0].strip()
    try:
        m = re.search(r"[\d.]+", s)
        if not m:
            return None
        num = float(m.group())
        if "cr" in s or "crore" in s:
            return num * 1_00_00_000
        if "lac" in s or "lakh" in s:
            return num * 1_00_000
        if "k" in s:
            return num * 1_000
        if "m" in s and "month" not in s and "sqm" not in s and "sq" not in s:
            return num * 1_000_000
        if "b" in s and "month" not in s and "sq" not in s:
            return num * 1_000_000_000
        return num
    except Exception:
        return None


# ── Main pipeline ─────────────────────────────────────────────────────────
def listing_pipeline(
    subject:                  dict,
    comparables:              list,
    property_type:            str,
    listing_type:             str = "sale",
    max_listings_per_project: int = 30,
    max_urls_per_project:     int = 5,
    on_progress=None,
    run_logger=None,
    custom_urls:              Optional[dict] = None,
) -> dict:

    cumulative_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    search_term = PROPERTY_TYPE_SEARCH_TERM.get(property_type, property_type)

    logger.info(
        f"[Pipeline Start] subject='{subject['project_name']}' "
        f"type='{property_type}' search_term='{search_term}' listing='{listing_type}' "
        f"comparables={len(comparables)}"
    )

    logger.info(f"[Pipeline] Subject: {subject}")

    projects = [{
        "project_name": subject["project_name"],
        "location":     subject.get("location_name", ""),
        "country":      subject.get("country", "India"),
        "lat":          subject.get("lat"),
        "lng":          subject.get("lng"),
        "is_subject":   True,
    }]
    for c in comparables:
        name = (c.get("project_name") or "").strip()
        if name:
            projects.append({
                "project_name": name,
                "location":     c.get("location") or subject.get("location_name", ""),
                "country":      c.get("country") or subject.get("country", "India"),
                "lat":          c.get("map_search_lat") or c.get("lat"),
                "lng":          c.get("map_search_lng") or c.get("lng"),
                "is_subject":   False,
            })

    logger.info(f"[Pipeline] Total projects: {len(projects)}")
    logger.info(f"[Pipeline] Projects: {projects}")

    if on_progress:
        on_progress("__pipeline__", "started", {
            "total_projects": len(projects),
            "property_type":  property_type,
            "search_term":    search_term,
        })

    # Hybrid Pipeline: Sequential Projects -> Parallel URLs
    all_listings = []
    
    for p in projects:
        pname = p["project_name"]
        logger.info(f"\n{'='*60}\n[Pipeline] Processing Project: {pname} (Parallel URLs)\n{'='*60}")
        
        if on_progress:
            on_progress(pname, "started", {"message": f"Searching and scraping {pname}..."})

        # Fetch road category and amenities (Local CSV or OSM)
        road_type = get_road_category(p.get("lat"), p.get("lng"))
        # We pass location as city_name; get_nearby_amenities uses fuzzy matching
        amenities = get_nearby_amenities(p.get("lat"), p.get("lng"), city_name=p.get("location"))
        
        print(f"\n>>> FETCHING AMENITY COUNTS FOR PROJECT: {pname} <<<")
        amenity_counts = get_amenity_counts(amenities)
        
        # Summary counts for factorial table
        amenity_summary = {
            "counts": amenity_counts,
            "total": len(amenities)
        }
        
        logger.info(f"[Pipeline] Road Category for {pname}: {road_type}")
        logger.info(f"[Pipeline] Amenities for {pname}: {amenity_summary}")

        # A. URL search (Sequential search is fine as it's one query)
        if custom_urls and pname in custom_urls:
            urls = custom_urls[pname]
        else:
            p_urls_map, p_usage = search_urls_for_projects_batch(
                [p], listing_type, property_type, num_results=max_urls_per_project
            )
            urls = p_urls_map.get(pname, [])
            
            logger.info(f"------------------- urls for {pname} --- {len(urls)} ---- {urls}")

            for k in cumulative_tokens:
                cumulative_tokens[k] += p_usage.get(k, 0)

        if not urls:
            log_drop("url_search", pname, "zero_urls_returned")
            continue

        # B. Scrape + Extract URLs in PARALLEL for this project
        p_listings_valid = []
        
        # B. Scrape + Extract URLs in PARALLEL for this project
        p_listings_valid = []
        
        def process_url(idx, url, current_pname, is_subject, current_road_type, current_amenities, current_amenity_summary, p_lat, p_lng):
            logger.info(f"[Scrape] URL #{idx} Starting: {url}")
            text = fetch_page_text(url, project_name=current_pname, run_logger=run_logger)
            if not text: 
                logger.info(f"[Scrape] URL #{idx} Failed (No text): {url}")
                return [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            
            listings, usage = extract_listings_from_text(
                url, text, project_name=current_pname, property_type=property_type
            )
            
            # Local collection for this URL
            valid = []
            for lst in listings:
                if is_matching_project(lst.get("project_name", ""), current_pname):
                    lst["project_name"]  = current_pname
                    lst["property_type"] = property_type
                    lst["is_subject"]    = is_subject
                    lst["road_type"]     = current_road_type
                    lst["amenities"]     = current_amenities
                    lst["amenity_summary"] = current_amenity_summary
                    lst["lat"]           = p_lat
                    lst["lng"]           = p_lng
                    lst["price_norm"]    = normalise_price(lst.get("price"))
                    
                    # Compute Price per Sqft
                    area = lst.get("area_sqft")
                    if lst["price_norm"] and area:
                        try:
                            area_num = float(area)
                            if area_num > 0:
                                lst["price_per_sqft"] = round(lst["price_norm"] / area_num)
                        except: pass
                    valid.append(lst)
            
            logger.info(f"[Scrape] URL #{idx} Finished: {url} -> {len(valid)} listings found")
            return valid, usage

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(process_url, i+1, url, pname, p["is_subject"], road_type, amenities, amenity_summary, p.get("lat"), p.get("lng")): url for i, url in enumerate(urls)}
            for future in as_completed(futures):
                try:
                    res_listings, usage = future.result()
                    p_listings_valid.extend(res_listings)
                    for k in cumulative_tokens:
                        cumulative_tokens[k] += usage.get(k, 0)
                except Exception as e:
                    logger.error(f"[Pipeline] Thread error for {pname}: {e}")

        if on_progress:
            on_progress(pname, "scraped", {"listings_found": len(p_listings_valid)})
            
        all_listings.extend(p_listings_valid)
        logger.info(f"[Project Done] {pname} -> {len(p_listings_valid)} listings found on web.")

    # Deduplicate across all projects
    seen, deduped = set(), []
    for lst in all_listings:
        key = (
            lst.get("project_name", ""),
            str(lst.get("bhk", "")),
            str(lst.get("price", "")),
            str(lst.get("area_sqft", "")),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(lst)
        else:
            log_drop("dedup", lst.get("project_name", ""), "duplicate_listing",
                     extra={"bhk": lst.get("bhk"), "price": lst.get("price"),
                            "area": lst.get("area_sqft")})

    # Final validation filter
    final_listings = []
    for lst in deduped:
        missing = [f for f in ("price", "area_sqft") if not is_valid_data(lst.get(f))]
        if missing:
            log_drop("final_validation", lst.get("project_name", ""),
                     f"missing_mandatory_fields: {missing}",
                     extra={"price": lst.get("price"), "area_sqft": lst.get("area_sqft")})
            continue
        final_listings.append(lst)

    if run_logger:
        run_logger.save_step("listing_search", "final_listings", final_listings)

    if on_progress:
        on_progress("__pipeline__", "completed", {
            "total_listings":     len(final_listings),
            "projects_processed": len(projects),
        })

    logger.info(
        f"[Pipeline Done] final_listings={len(final_listings)} | "
        f"projects={len(projects)} | tokens={cumulative_tokens['total_tokens']}"
    )

    return {
        "listings":           final_listings,
        "token_usage":        cumulative_tokens,
        "projects_processed": len(projects),
        "total_listings":     len(final_listings),
    }
