# Web Search Subsystem Documentation

This document explains the backend web-search code path used by the agentic AI backend. It covers the modules currently involved in search, URL discovery, crawling, content extraction, document downloading, caching, and answer generation.

## 1. High-Level Purpose

The web-search subsystem answers user questions by:

1. Receiving a query from an API route or web UI.
2. Checking whether a recent cached answer already exists.
3. Understanding the query and generating focused search query variants.
4. Searching the web through free/no-key sources.
5. Ranking and filtering URLs for relevance.
6. Fetching content from selected URLs.
7. Optionally crawling same-domain links and downloading linked documents.
8. Extracting structured evidence, facts, numbers, dates, locations, and exact matches.
9. Producing a final answer through an LLM when available, or a deterministic source-based fallback.
10. Returning the answer, source list, metadata, token usage, and optional debug payloads.

The main orchestrator is:

```text
agents/web_search/main.py
```

The main supporting modules are:

```text
tools/web_search/search.py
tools/web_search/discovery.py
tools/web_search/browser.py
tools/web_search/crawler.py
tools/web_search/document_downloader.py
database/web_search/cache.py
agents/web_search/prompts.py
core/web_search/config.py
api/routes/web_search/*.py
```

## 2. Runtime Flow

### 2.1 API Entry Points

The web-search API routes are in:

```text
api/routes/web_search/search.py
api/routes/web_search/chat.py
api/routes/web_search/web.py
api/routes/web_search/health.py
```

#### `/api/search`

Defined in `api/routes/web_search/search.py`.

This endpoint calls:

```python
agent.search(query, max_results, use_cache=not no_cache)
```

Important parameters:

- `query`: User search query.
- `max_results`: Number of results requested. Defaults to `config.MAX_RESULTS_IN_RESPONSE`.
- `no_cache`: If `true`, bypasses cached responses.

#### `/api/chat_stream`

Defined in `api/routes/web_search/chat.py`.

This endpoint streams progress and answer chunks through server-sent events.

It calls:

```python
agent.search(
    query,
    max_results=config.MAX_RESULTS_IN_RESPONSE,
    use_cache=not no_cache,
    status_callback=status_callback,
    stream_callback=stream_callback,
    debug_llm_payloads=debug,
)
```

The web UI in `api/routes/web_search/web.py` currently sends:

```javascript
/api/chat_stream?query=...&no_cache=true
```

That means the browser chat flow bypasses cache by default.

#### `/api/extract`

Defined in `api/routes/web_search/search.py`.

This endpoint extracts information from a single URL:

```python
agent.extract_from_url(url, query)
```

#### `/health`

Defined in `api/routes/web_search/health.py`.

Returns basic flags:

```json
{
  "status": "healthy",
  "llm_enabled": true,
  "cache_enabled": true
}
```

## 3. Configuration

Configuration lives in:

```text
core/web_search/config.py
```

The `Config` class reads environment variables through `dotenv`.

### 3.1 Search Settings

```python
DDG_MAX_RESULTS
DDG_TIMEOUT
DDG_REGION
```

These control DuckDuckGo-related behavior.

### 3.2 LLM Settings

```python
OPENAI_API_KEY
USE_LLM
LLM_MODEL
MAX_TOKENS
```

If `OPENAI_API_KEY` is present, `USE_LLM` becomes true and the analyzer/planner can call OpenAI.

### 3.3 Content Processing

```python
MAX_CONTENT_LENGTH
EXTRACT_READABLE
```

`MAX_CONTENT_LENGTH` limits extracted text passed around the pipeline.

### 3.4 Crawling and Document Extraction

```python
ENABLE_CRAWLING
CRAWL_MAX_DEPTH
CRAWL_MAX_PAGES
CRAWL_TOP_RESULTS
CRAWL_TIMEOUT
USE_LLM_GUIDED_CRAWL
ENABLE_DOCUMENT_DOWNLOAD
DOCUMENT_DOWNLOAD_DIR
DOCUMENT_MAX_DOWNLOADS
DOCUMENT_MAX_BYTES
```

These control whether the agent follows links and downloads linked documents.

### 3.5 Cache Settings

```python
CACHE_ENABLED
CACHE_TTL
CACHE_DIR
```

Defaults:

```python
CACHE_ENABLED = true
CACHE_TTL = 1800
CACHE_DIR = "data/cache"
```

## 4. Main Agent Orchestrator

File:

```text
agents/web_search/main.py
```

Main class:

```python
class DuckDuckGoSearchAgent
```

Despite the name, this agent now coordinates more than DuckDuckGo. It uses `SourceDiscovery`, content extraction, optional crawling, document downloading, and answer generation.

### 4.1 Initialization

The constructor creates:

```python
self.searcher = DuckDuckGoSearcher()
self.discovery = SourceDiscovery(self.searcher)
self.analyzer = LightweightAnalyzer()
self.processor = ContentProcessor()
self.crawler = LLMGuidedCrawler(...) or WebCrawler()
self.downloader = DocumentDownloader()
self.cache = SearchCache() if config.CACHE_ENABLED else None
self.validator = AccuracyValidator()
```

Purpose of each component:

- `DuckDuckGoSearcher`: Free web search provider wrapper.
- `SourceDiscovery`: Query rewriting, candidate URL collection, and relevance ranking.
- `LightweightAnalyzer`: LLM/fallback answer generation.
- `ContentProcessor`: Fetches pages and extracts useful text/evidence.
- `WebCrawler` / `LLMGuidedCrawler`: Follows same-domain links from top sources.
- `DocumentDownloader`: Downloads PDFs, DOCX, XLSX, TXT, etc.
- `SearchCache`: Stores complete output for repeated queries.
- `AccuracyValidator`: Cross-validates extracted structured data.

### 4.2 Main Search Method

Method:

```python
search(query, max_results=10, fetch_content=True, use_cache=True, ...)
```

Steps:

1. Build cache key:

   ```python
   cache_query = f"{SEARCH_CACHE_VERSION}:{query}"
   ```

2. If cache is enabled and `use_cache=True`, try to return cached output.

3. Run discovery:

   ```python
   discovery = self.discovery.discover(query, max_results, ...)
   ```

4. Convert discovery results into dictionaries.

5. Fetch full content for top results:

   ```python
   self.processor.process_batch(urls, query=query)
   ```

6. Merge extracted content and metadata into result objects.

7. If crawling is enabled, crawl top results:

   ```python
   self._crawl_and_extract_documents(query, results_dict)
   ```

8. Generate final answer:

   ```python
   self.analyzer.generate_trusted_answer(...)
   ```

   or fallback:

   ```python
   self.analyzer.build_source_based_answer(...)
   ```

9. Add token usage, metadata, and timestamps.

10. Save output to cache if enabled.

### 4.3 Output Shape

Successful search returns:

```python
{
    "query": query,
    "success": True,
    "discovery": ...,
    "discovery_token_usage": ...,
    "results_count": len(results_dict),
    "results": results_dict,
    "analysis": analysis,
    "accuracy": output_metadata,
    "timestamp": datetime.now().isoformat(),
    "token_usage": token_report,
}
```

Each result may include:

```python
{
    "url": "...",
    "title": "...",
    "snippet": "...",
    "rank": 1,
    "source": "bing|searxng|wikipedia|duckduckgo-html|...",
    "search_query": "...",
    "matched_entities": [],
    "relevance_score": 0.75,
    "content": "...",
    "published_date": "...",
    "time_ago": "...",
    "source_trust": 0.8,
    "exact_ready_reckoner_rows": [],
    "exact_evidence_matches": [],
    "extracted_data": ...
}
```

## 5. Search Cache

File:

```text
database/web_search/cache.py
```

Class:

```python
class SearchCache
```

### 5.1 Purpose

The cache avoids repeating expensive work for the same query:

- Web searches.
- Page fetching.
- Crawling.
- Document downloading.
- Content extraction.
- LLM answer generation.

This improves speed and reduces API/token usage.

### 5.2 Storage

Uses `diskcache.Cache`.

Directory:

```python
config.CACHE_DIR
```

Default:

```text
data/cache
```

### 5.3 Cache Key

Cache key is generated with:

```python
key_string = f"{search_type}:{query.lower().strip()}"
hashlib.md5(key_string.encode()).hexdigest()
```

The agent passes a versioned query:

```python
source-discovery-v9-crawl-docs:{query}
```

The version string lets developers invalidate old cache entries by changing `SEARCH_CACHE_VERSION`.

### 5.4 Read Path

```python
cached = self.cache.get(cache_query)
```

If a valid entry exists and is younger than `CACHE_TTL`, it returns cached output immediately.

### 5.5 Write Path

```python
self.cache.set(cache_query, output)
```

The whole search output is cached:

```python
{
    "timestamp": time.time(),
    "data": output,
    "query": query,
    "type": search_type
}
```

### 5.6 Clearing Cache

Clear all:

```python
SearchCache().clear()
```

Clear entries older than N seconds:

```python
SearchCache().clear(older_than=3600)
```

For the default 30-minute TTL:

```python
SearchCache().clear(older_than=1800)
```

Disable cache through `.env`:

```env
CACHE_ENABLED=false
```

Bypass cache per request:

```text
/api/search?query=...&no_cache=true
/api/chat_stream?query=...&no_cache=true
```

## 6. URL Search Providers

File:

```text
tools/web_search/search.py
```

Main data type:

```python
@dataclass
class SearchResult
```

Fields:

```python
url
title
snippet
source
rank
content
relevance_score
quality_score
content_type
fetch_time
word_count
has_date
domain_authority
is_recent
```

### 6.1 DuckDuckGoSearcher

Class:

```python
class DuckDuckGoSearcher
```

Despite the name, this class uses multiple free/no-key search sources.

Current provider flow:

1. Bing.
2. SearXNG.
3. Wikipedia OpenSearch.
4. DuckDuckGo HTML.
5. `duckduckgo_search` package fallback.

The method:

```python
search(query, max_results=5)
```

collects results from providers until enough unique URLs are found.

### 6.2 Request Headers

`_headers()` rotates user agents and sends browser-like headers:

```python
User-Agent
Accept
Accept-Language
Accept-Encoding
Connection
DNT
Upgrade-Insecure-Requests
Referer
```

Purpose:

- Reduce bot-blocking.
- Improve chances of getting normal HTML.
- Avoid compressed responses by using `Accept-Encoding: identity`.

### 6.3 Bing Search

Method:

```python
_search_bing(query, max_results)
```

Uses Bing result pages and parses `li.b_algo` result blocks.

Extracts:

- Title.
- URL.
- Snippet.

Filters:

- Bing internal URLs.
- Some Chinese domains.

### 6.4 SearXNG Search

Method:

```python
_search_searxng(query, max_results)
```

Uses public SearXNG instances:

```python
https://searx.be
https://searxng.world
https://search.inetol.net
https://searx.tiekoetter.com
https://opnxng.com
https://paulgo.io
https://searx.bnyro.com
```

It shuffles instances and stops after the first successful one.

### 6.5 Wikipedia Search

Method:

```python
_search_wikipedia(query, max_results=3)
```

Uses Wikipedia OpenSearch API.

This is useful for factual/general knowledge queries, but less useful for niche real-estate or live market queries.

### 6.6 DuckDuckGo HTML Search

Method:

```python
_search_duckduckgo_html(query, max_results)
```

Uses:

```text
https://html.duckduckgo.com/html/
```

It warms up the session by calling:

```text
https://duckduckgo.com/
```

Then parses HTML result blocks and extracts the real URL from DuckDuckGo redirect parameter `uddg`.

### 6.7 DDGS Package Fallback

Method:

```python
_search_ddgs_package(ddgs, query, max_results)
```

Uses the installed `duckduckgo_search` package if available.

It has retry behavior for rate-limit-like errors.

### 6.8 Deduplication

Method:

```python
_dedupe_results(results)
```

Dedupes by:

```python
domain + path
```

It strips `www.` and trailing slashes.

## 7. Source Discovery and Ranking

File:

```text
tools/web_search/discovery.py
```

Main class:

```python
class SourceDiscovery
```

Purpose:

- Understand query intent.
- Generate search query variants.
- Execute searches.
- Filter and rank relevant URLs.

### 7.1 QueryUnderstanding

Dataclass:

```python
class QueryUnderstanding
```

Fields:

```python
original_query
intent
key_entities
rewritten_queries
positive_terms
avoid_terms
used_llm
is_real_estate
```

This is returned inside final output under `discovery`.

### 7.2 Query Understanding

Method:

```python
understand_query(query)
```

If LLM is available, it calls:

```python
_understand_query_with_llm()
```

Otherwise it uses deterministic parsing:

- Normalize whitespace.
- Extract key entities.
- Detect basic intent.
- Build search queries.
- Build positive terms.

### 7.3 LLM Query Planner

Method:

```python
_understand_query_with_llm(query)
```

Asks the model to return JSON:

```json
{
  "intent": "...",
  "key_entities": [],
  "synonyms": [],
  "search_queries": [],
  "positive_terms": [],
  "avoid_terms": []
}
```

The output is parsed into `QueryUnderstanding`.

### 7.4 Real Estate Detection

Class:

```python
RealEstateIntentDetector
```

Detects real-estate-specific queries using:

- Project terms.
- Status terms.
- Feature terms.
- Location terms.
- Ready-reckoner / circle-rate / valuation terms.

It also blocks tourism domains and tourism-like content for real-estate queries.

### 7.5 Property Rate Query Handling

Property-rate query terms include:

```text
ready reckoner
reckoner rate
circle rate
government valuation
property rate
guideline value
market value
annual statement of rates
asr rate
```

For these queries, `generate_project_search_queries()` adds targeted searches such as:

```text
ready reckoner rate {location} survey no {survey_number}
{location} survey no {survey_number} ready reckoner rate
annual statement of rates {location} survey no {survey_number}
government valuation {location} survey no {survey_number}
ready reckoner rate {location}
ready reckoner rate {location} 2026
ready reckoner rate {location} haveli pune
```

### 7.6 Discovery Flow

Method:

```python
discover(query, max_results=5)
```

Flow:

1. Reset token usage.
2. Understand the query.
3. Detect real-estate intent.
4. Add specific constraint-based queries.
5. Add real-estate/project/property-rate query variants.
6. Search each query variant.
7. Deduplicate URLs.
8. Validate real-estate sources if needed.
9. Rank each result.
10. Filter off-topic sources.
11. Optionally rerank with LLM.
12. Select top `max_results`.

### 7.7 Result Ranking

Method:

```python
_rank_result(result, understanding, search_query)
```

Builds a `haystack`:

```python
title + snippet + url
```

Then computes overlap with important query terms.

For property-rate queries:

- If the result looks like a property-rate result, relevance gets boosted.
- Otherwise relevance is reduced.

### 7.8 Trust Scoring

Method:

```python
_calculate_source_trust(url)
```

Starts at `0.50`.

Boosts:

- Government domains.
- Education domains.
- Short/simple domains.

Returns value between `0.25` and `0.95`.

### 7.9 Filtering

Method:

```python
filter_relevant_sources(results, query)
```

Filters:

- Blocked tourism domains.
- Tourism-like content for real-estate queries.
- Non-property-rate results for property-rate queries.

## 8. Content Extraction

File:

```text
tools/web_search/browser.py
```

Main class:

```python
class ContentProcessor
```

Purpose:

- Fetch HTML.
- Extract readable text.
- Extract table content.
- Extract exact matches.
- Extract facts, numbers, dates, locations, and entities.
- Estimate confidence and trust.

### 8.1 Fetching HTML

Method:

```python
fetch_html(url, timeout=15)
```

Uses `requests.Session`.

Decorated with tenacity retry:

```python
@retry(stop=stop_after_attempt(2), wait=wait_exponential(...))
```

If fetching fails, returns `None`.

### 8.2 Multi-Strategy Extraction

Method:

```python
_try_all_extraction_methods(html, url)
```

It tries:

1. HTML table extraction.
2. `trafilatura`.
3. `readability`.
4. BeautifulSoup main-content fallback.

Each candidate extraction gets:

```python
method
content
length
quality_score
```

### 8.3 Table Extraction

Method:

```python
_extract_tables_as_text(html)
```

Converts tables into line-based text:

```text
Header1 | Header2 | Header3
Cell1 | Cell2 | Cell3
```

This is important for government rate tables, valuation tables, fees, and schedules.

### 8.4 Exact Ready-Reckoner Rows

Method:

```python
_extract_exact_ready_reckoner_rows(content, query)
```

Looks for survey numbers requested in the query:

```text
survey no 28
gat no 12
cts 123
plot no 5
```

If a line contains the requested identifier, it records:

```python
{
    "survey_numbers": [...],
    "row_text": "..."
}
```

### 8.5 Exact Evidence Matches

Method:

```python
_extract_exact_evidence_matches(content, query)
```

Finds lines that contain:

- Quoted query phrases.
- Named phrases.
- Years.
- IDs.
- Model/version-like tokens.
- Specific numeric values.
- Enough important query terms.

Returns:

```python
{
    "matched_constraints": [...],
    "matched_terms": [...],
    "text": "..."
}
```

### 8.6 Best Extraction Selection

Method:

```python
_select_best_extraction(results, query)
```

Scores extraction candidates using:

```python
final_score = quality_score * 0.6 + relevance_score * 0.4
```

The best candidate becomes an `ExtractedData` object.

### 8.7 Structured Data

Method:

```python
_extract_structured_data(html)
```

Looks for JSON-LD / Schema.org metadata.

If found, it merges structured fields into extracted data through:

```python
_merge_structured_data()
```

### 8.8 Facts, Numbers, Dates, Locations, Entities

Methods:

```python
_extract_key_facts()
_extract_numbers()
_extract_dates()
_extract_locations()
_extract_entities()
```

These build supporting evidence used later by validation and answer generation.

### 8.9 Confidence and Source Trust

Methods:

```python
_calculate_confidence()
_infer_source_trust()
```

Factors include:

- Extraction quality.
- Query relevance.
- Source type.
- Whether exact data was found.
- Domain type.

### 8.10 Batch Processing

Method:

```python
process_batch(urls, query="", delay=1.0, status_callback=None)
```

Fetches and extracts from multiple URLs.

Returns a list of dictionaries with extracted content and metadata.

## 9. Crawling

File:

```text
tools/web_search/crawler.py
```

Main classes:

```python
WebCrawler
LLMGuidedCrawler
```

### 9.1 Purpose

The crawler enriches top search results by following same-domain links that may contain:

- Detail pages.
- PDF links.
- Data pages.
- Related reports.
- Download pages.

### 9.2 WebCrawler Initialization

Config-driven defaults:

```python
max_depth = config.CRAWL_MAX_DEPTH
max_pages = config.CRAWL_MAX_PAGES
timeout = config.CRAWL_TIMEOUT
```

It maintains:

```python
self.visited
self.results
```

### 9.3 crawl()

```python
await crawl(start_url, query_context="")
```

Crawls one start URL.

### 9.4 crawl_many()

```python
await crawl_many(start_urls, query_context="")
```

Crawls multiple start URLs until `max_pages` is reached.

### 9.5 Recursive Crawl

Method:

```python
_crawl_recursive(url, depth, query_context)
```

Stops if:

- `depth > max_depth`
- `len(results) >= max_pages`
- URL already visited
- URL is a document file

For each HTML page:

1. Fetch HTML.
2. Parse with BeautifulSoup.
3. Extract title.
4. Extract text.
5. Extract same-domain links.
6. Extract document links.
7. Save result.
8. Follow relevant links.

### 9.6 Link Filtering

Only same-domain links are crawled.

External document links may still be collected as `document_links`.

Skipped:

- `javascript:`
- `mailto:`
- `tel:`
- Binary assets like images, archives, audio/video.

### 9.7 Relevance Heuristic

Method:

```python
_is_relevant_link(url, query_context)
```

If fewer than 5 pages have been collected, it allows links more broadly.

After that, it scores link paths using:

- Query keywords.
- Relevance terms like `download`, `document`, `pdf`, `rates`, `report`, `data`, `details`.

### 9.8 LLMGuidedCrawler

Subclass:

```python
class LLMGuidedCrawler(WebCrawler)
```

If enabled, asks the LLM:

```text
Given this search query/context, decide if this URL is likely to contain specific useful information.
Respond with only YES or NO.
```

If LLM call fails, it falls back to the deterministic heuristic.

## 10. Document Downloading

File:

```text
tools/web_search/document_downloader.py
```

Main class:

```python
class DocumentDownloader
```

### 10.1 Purpose

Downloads and extracts text from documents discovered during crawling.

Supported extensions:

```text
pdf
doc
docx
xls
xlsx
ppt
pptx
txt
```

The downloader only extracts text from:

```text
pdf
doc/docx
xls/xlsx
txt
```

PowerPoint files are detected as document URLs but do not currently have a custom extractor.

### 10.2 Finding Documents

Method:

```python
find_and_download_documents(crawler_results)
```

Sources:

- `document_links` collected by the crawler.
- Raw URLs found inside crawled page content.

Dedupes URLs and limits to:

```python
config.DOCUMENT_MAX_DOWNLOADS
```

Before downloading a document, the downloader checks `SearchCache` with:

```python
search_type="document"
```

If the document was downloaded and extracted within the cache TTL, the cached document dictionary is reused and returned in the same output format. This avoids repeated downloads and repeated PDF/DOCX/XLSX extraction work.

If the cached entry points to a local filepath that no longer exists, the cache entry is ignored and the document is downloaded again so the on-disk file can be restored.

### 10.3 Download Limits

Before extracting, it checks:

```python
content-length <= max_bytes
len(data) <= max_bytes
```

Default max size comes from:

```python
config.DOCUMENT_MAX_BYTES
```

### 10.4 File Naming

Method:

```python
_safe_filename(url)
```

Uses:

- URL path filename if available.
- SHA1 digest suffix.
- Sanitized characters.

This avoids collisions and unsafe filenames.

### 10.5 Text Extraction

PDF:

1. Try `fitz` / PyMuPDF.
2. Fallback to `pypdf`.

DOCX:

```python
docx2txt.process()
```

Excel:

```python
pandas.read_excel(sheet_name=None, nrows=100)
```

Text:

```python
filepath.read_text(...)
```

### 10.6 Document Output

Each downloaded document returns:

```python
{
    "url": url,
    "filename": filename,
    "filepath": filepath,
    "source_url": source_url,
    "size": len(data),
    "content": extracted_text,
    "content_type": "pdf|docx|xlsx|txt|document"
}
```

## 11. Answer Generation

File:

```text
agents/web_search/prompts.py
```

Main class:

```python
class LightweightAnalyzer
```

### 11.1 Purpose

Generates the final answer using:

- LLM if configured.
- Deterministic source-based fallback if no LLM is available or LLM fails.

### 11.2 needs_analysis()

If an OpenAI client exists, always uses LLM.

If no LLM is available:

- Simple queries with few results can use fallback.
- Complex queries prefer analysis, but fallback is still used if no client exists.

### 11.3 generate_trusted_answer()

Flow:

1. If no results, return a helpful failure message.
2. Cross-validate extracted objects with `AccuracyValidator`.
3. Build an accuracy prompt.
4. Generate answer through LLM, streamed or non-streamed.
5. If LLM fails, use `build_source_based_answer()`.
6. Return:

   ```python
   {
       "answer": answer,
       "accuracy_score": ...,
       "validated_claims": ...,
       "sources_agreed": ...,
       "recommendation": ...,
       "confidence_level": ...
   }
   ```

### 11.4 Source-Based Fallback

Method:

```python
build_source_based_answer(query, results)
```

Produces Markdown sections:

- Source-Based Answer.
- Exact Matches.
- Matching Existing Projects / Listings.
- Key Findings From Sources.
- Confidence Insight.
- Reference URLs.

This fallback avoids inventing missing data and uses only retrieved snippets/content.

### 11.5 Token Usage

The analyzer tracks:

```python
input_tokens
output_tokens
total_cost
query_count
```

Returned through:

```python
get_token_report()
```

## 12. Single URL Extraction

The agent supports direct URL extraction:

```python
extract_from_url(url, query)
```

Flow:

1. Fetch HTML with `ContentProcessor.fetch_html()`.
2. Extract with `extract_with_confidence()`.
3. Build answer from extracted content.
4. Return:

```python
{
    "query": query,
    "url": url,
    "success": True,
    "extracted_data": ...,
    "analysis": ...,
    "timestamp": ...
}
```

This is useful for debugging extraction without running search/discovery.

## 13. Data Flow Diagram

```text
User / Frontend
    |
    v
API Route
    |
    v
DuckDuckGoSearchAgent.search()
    |
    +--> SearchCache.get()
    |       |
    |       +--> Return cached output if valid
    |
    +--> SourceDiscovery.discover()
    |       |
    |       +--> QueryUnderstanding
    |       +--> Search query variants
    |       +--> DuckDuckGoSearcher.search()
    |       |       |
    |       |       +--> Bing
    |       |       +--> SearXNG
    |       |       +--> Wikipedia
    |       |       +--> DuckDuckGo HTML
    |       |       +--> DDGS fallback
    |       |
    |       +--> Rank/filter candidates
    |
    +--> ContentProcessor.process_batch()
    |       |
    |       +--> fetch_html()
    |       +--> extract_with_confidence()
    |       +--> exact rows / evidence matches
    |
    +--> WebCrawler.crawl_many()
    |       |
    |       +--> same-domain pages
    |       +--> document links
    |
    +--> DocumentDownloader.find_and_download_documents()
    |       |
    |       +--> PDFs / DOCX / XLSX / TXT
    |
    +--> LightweightAnalyzer.generate_trusted_answer()
    |       |
    |       +--> AccuracyValidator.cross_validate()
    |       +--> LLM or fallback answer
    |
    +--> SearchCache.set()
    |
    v
API Response / Streamed Answer
```

## 14. Important Runtime Behaviors

### 14.1 Cache Can Hide Code Changes

If you change search/ranking/extraction code but reuse the same query, cache may return an old answer.

Use:

```text
no_cache=true
```

or change:

```python
SEARCH_CACHE_VERSION
```

or clear:

```python
SearchCache().clear()
```

### 14.2 Search Providers Are Not Guaranteed

Free providers may:

- Rate-limit.
- Return no results.
- Change HTML structure.
- Block bot-like traffic.
- Return regionally different results.

That is why the system uses several providers and fallbacks.

### 14.3 URL Discovery and Content Evidence Are Different

A URL can look relevant from title/snippet but fail content extraction.

The pipeline separates:

- Search relevance.
- Source trust.
- Full content extraction.
- Exact evidence extraction.
- Final answer confidence.

### 14.4 Crawling Is Same-Domain Only

`WebCrawler` follows same-domain links only. It may collect external document links but does not crawl arbitrary external HTML pages from those links.

This prevents crawl explosion and keeps the search bounded.

### 14.5 Documents Are Saved Locally

Downloaded files are stored under:

```python
config.DOCUMENT_DOWNLOAD_DIR
```

Default:

```text
data/web_documents
```

These files may need periodic cleanup.

## 15. Troubleshooting

### Problem: Same wrong answer keeps appearing

Likely cause:

- Cached old output.

Fix:

```text
no_cache=true
```

or:

```python
SearchCache().clear()
```

or set:

```env
CACHE_ENABLED=false
```

### Problem: Only one or two URLs are shown

Possible causes:

- Search providers returned few results.
- Relevance filtering removed weak results.
- Real-estate/property-rate filters removed listing or tourism pages.
- Crawling/document extraction failed.
- The UI only displays `results[:10]`, so if backend result count is low the UI will show fewer.

### Problem: Search provider returns zero

Possible causes:

- Rate limit.
- DNS/network issue.
- Search HTML changed.
- Public SearXNG instance down.

Fixes:

- Try again.
- Use broader query.
- Add more SearXNG instances.
- Add domain-specific seed URLs for known sources.

### Problem: Exact survey/property-rate row not found

Possible causes:

- Search found a listing page, not official rate table.
- Table content is loaded dynamically.
- Extractor selected article text instead of table text.
- The requested survey number is not present in retrieved content.

Fixes:

- Prefer official/known ready-reckoner URLs.
- Improve table extraction.
- Add dynamic AJAX handling for the specific site.
- Fetch more than top 5 results.

### Problem: LLM answer invents details

Fixes:

- Strengthen prompt rules.
- Prefer source-based fallback for exact-data queries.
- Require citations from exact evidence rows.
- Add post-processing that rejects claims not present in extracted evidence.

## 16. Suggested Improvements

1. Add first-class `evidence_level` to every result.
2. Separate `candidate_urls` from `evidence_urls`.
3. Add provider diagnostics to final response.
4. Add cache endpoint:

   ```text
   POST /api/cache/clear
   GET /api/cache/stats
   ```

5. Add tests for:

   - Cache hit/miss behavior.
   - Query rewriting.
   - URL deduplication.
   - Property-rate filtering.
   - Exact survey-number extraction.
   - Document downloading size limits.

6. Add per-provider timeout and failure counters.
7. Store search provider raw results in debug mode.
8. Add source allowlist/seed configuration for known government datasets.

## 17. Quick Developer Commands

Run syntax check:

```powershell
.\venv\Scripts\python.exe -m py_compile `
  agents/web_search/main.py `
  tools/web_search/search.py `
  tools/web_search/discovery.py `
  tools/web_search/browser.py `
  tools/web_search/crawler.py `
  tools/web_search/document_downloader.py `
  database/web_search/cache.py
```

Run a direct URL search smoke test:

```powershell
.\venv\Scripts\python.exe -X utf8 -c "from tools.web_search.search import DuckDuckGoSearcher; s=DuckDuckGoSearcher(); rs=s.search('ready reckoner rate Baner survey no 28', max_results=10); print(len(rs)); [print(r.source, r.title, r.url) for r in rs]"
```

Clear cache:

```powershell
.\venv\Scripts\python.exe -c "from database.web_search.cache import SearchCache; SearchCache().clear()"
```

Check cache stats:

```powershell
.\venv\Scripts\python.exe -c "from database.web_search.cache import SearchCache; print(SearchCache().stats())"
```

## 18. Glossary

- **Discovery**: Finding and ranking candidate URLs.
- **Crawler**: Follows links from selected pages.
- **Document downloader**: Downloads linked PDFs/DOCX/XLSX/TXT and extracts text.
- **Extraction**: Turns HTML or documents into usable text/evidence.
- **Exact evidence**: Lines that contain requested identifiers, numbers, or constraints.
- **Cache**: Persistent disk storage for full search outputs.
- **LLM analysis**: Model-generated final answer using retrieved source context.
- **Source-based fallback**: Deterministic answer built from retrieved content without LLM.
