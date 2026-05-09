"""
ReAct Query Builder Agent  —  v3.0
====================================
Clean architecture: User Query + Schema → SQL. Nothing else pre-filters the intent.

The previous design passed tool_selections and plan steps into the SQL builder,
which caused the builder to only filter on the first entity it saw (e.g. Hinjewadi)
and silently discard the rest (e.g. Baner). That pre-filtering layer is gone.

New pipeline
─────────────
  User Query (raw string)
       │
       ▼
  [1] IntentExtractor      — NL → structured intent dict
       │                     reads: user_query + schema
       │                     writes: analysis_type, metrics, entities (ALL of them),
       │                             filters, group_by, order_by, time_series
       ▼
  [2] TransactionQueryBuilder.run(intent)
       │
       ├── BUILD    — schema-grounded SQL; ALL intent entities in WHERE clause
       ├── REVIEW   — pre-execution gate; entity completeness check is #1 priority
       ├── EXECUTE  — PostgreSQL via db_executor
       ├── OBSERVE  — checks result for missing entities, wrong column, wrong gran
       └── REFLECT  — LLM-driven column fallback (schema as ground truth; no hardcoded map)
           └── REWRITE → back to EXECUTE …  (up to MAX_ITERATIONS)

Top-level entry point
──────────────────────
  result = run_query(user_query, client, db_executor)

What was removed vs v2
───────────────────────
  • tool_selections parameter   — caused single-entity filtering
  • plan steps injection        — narrowed scope before SQL was built
  • COLUMN_ALIAS_MAP            — replaced by LLM schema reasoning
  • registry dependency         — not needed without tool selection layer
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from openai import OpenAI

from agents.data_retrieval_transaction.constants import MAX_ITERATIONS, REVIEW_SAMPLE
from agents.data_retrieval_transaction.helpers import (
    clean_sql,
    extract_filter_columns,
    parse_json,
    validate_select_only,
)
from agents.data_retrieval_transaction.intent_extractor import IntentExtractor
from agents.data_retrieval_transaction.models import (
    Iteration,
    ObserveVerdict,
    QueryResult,
    StepStatus,
)
from agents.data_retrieval_transaction.prompts import (
    INTENT_EXTRACT_PROMPT,
    SQL_BUILD_PROMPT,
    SQL_FIX_PROMPT,
    SQL_OBSERVE_PROMPT,
    SQL_PROBE_PROMPT,
    SQL_REFLECT_PROMPT,
    SQL_REVIEW_PROMPT,
)
from agents.data_retrieval_transaction.schema import TRANSACTION_QUERY_SCHEMA

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

MAX_ITERATIONS: int = 5
REVIEW_SAMPLE:  int = 5

SPACE_FILTER_FIELD_ORDER: tuple[str, ...] = (
    "unit_number",
    "tower_name",
    "plot_number",
    "project_name",
    "location_name",
    "micro_market",
    "city",
    "city_name",
    "state_name",
    "country_name",
    "sub_locality",
    "village_name",
    "pincode",
)

SPACE_OPTION_TO_FIELD: dict[str, str] = {
    "unit": "unit_number",
    "building": "tower_name",
    "plot_number": "plot_number",
    "project": "project_name",
    "location": "location_name",
    "micromarket": "micro_market",
    "city": "city_name",
    "state": "state_name",
    "country": "country_name",
}


# ══════════════════════════════════════════════════════════════════════════════
# Enums & data classes
# ══════════════════════════════════════════════════════════════════════════════

class StepStatus(str, Enum):
    EXTRACT  = "extract"
    BUILD    = "build"
    REVIEW   = "review"
    EXECUTE  = "execute"
    OBSERVE  = "observe"
    REFLECT  = "reflect"
    REWRITE  = "rewrite"
    DONE     = "done"
    FAILED   = "failed"


class ObserveVerdict(str, Enum):
    GOOD              = "good"
    EMPTY             = "empty"
    DB_ERROR          = "db_error"
    WRONG_COLUMN      = "wrong_column"
    WRONG_GRANULARITY = "wrong_gran"
    IRRELEVANT        = "irrelevant"


@dataclass
class Iteration:
    index:          int
    sql:            str
    status:         StepStatus
    error:          str | None            = None
    rows:           list[dict]            = field(default_factory=list)
    verdict:        ObserveVerdict | None = None
    review:         dict                  = field(default_factory=dict)
    reflect:        dict                  = field(default_factory=dict)
    columns_tried:  list[str]             = field(default_factory=list)
    duration_ms:    int                   = 0
    usage:          Any                   = None


@dataclass
class QueryResult:
    sql:         str
    rows:        list[dict]
    intent:      dict
    iterations:  int
    trace:       list[Iteration]
    success:     bool
    usage:       Any                   = None
    error:       str | None            = None


# ══════════════════════════════════════════════════════════════════════════════
# Prompt: Intent Extraction
# ══════════════════════════════════════════════════════════════════════════════

INTENT_EXTRACT_PROMPT = """
You are an intent extraction agent for a real-estate intelligence platform.

Convert the user's natural language query into a structured JSON intent object.
This intent drives SQL generation — it must capture EVERYTHING the user asked for.

=============================================================
EXTRACTION RULES
=============================================================
1.  Capture EVERY entity the user mentions without exception.
    If the user says "compare Baner and Hinjewadi", both must appear
    in entities.locations. Never silently drop any entity.
    If the user mentions a unit, building/tower, parcel/survey/CTS/khasra/plot
    number, project, location/locality, micromarket, city, state, or country,
    capture it in entities.space_filters using the matching schema field.

2.  Infer analysis_type:
    - "compare X and Y / X vs Y"   → "comparison"
    - "trend / over time / by year" → "trend"
    - "top N / rank / best"         → "ranking"
    - "how many / breakdown / split"→ "distribution"
    - "total / average / overall"   → "summary"
    - "show / list / find"          → "lookup"

3.  Infer ALL metrics the user asked for:
    - "total sales / total value"        → SUM(agreement_price)   alias total_sales_value
    - "units sold / transactions / count"→ COUNT(*)               alias units_sold
    - "rate per sqft / price per sqft"   → SUM(ap)/SUM(area)*conv alias rate_per_sq_ft
      NOTE: DB stores area as net_carpet_area_sq_m.
            rate_per_sq_ft = rate_per_sq_m / 10.764
    - "average price"                    → AVG(agreement_price)   alias avg_price
    Multiple metrics → list all of them.

4.  For each location entity, infer semantic_level:
    - Neighbourhood/locality (Baner, Wakad, Hinjewadi, Kothrud…) → "locality"
    - City (Pune, Mumbai, Nagpur…)                                → "city"
    - Project / building name                                      → "project"
    - Developer / builder name                                     → "developer"
    - Property configuration (2BHK, 3BHK, Studio…)                → "property_type"

5.  Infer standard filters:
    - "residential sales" → transaction_category: "sale"
    - Year / quarter if explicitly mentioned, else null.
    - Property type if mentioned.

6.  Do not add filters the user did not mention.

7.  If the query does not specify any unit, building/tower, parcel/survey/CTS/
    khasra/plot number, project, location/locality, micromarket, city, state,
    or country, set route to "clarify", needs_clarification to true, and ask
    which space should be used.

=============================================================
SCHEMA  (for understanding available dimensions)
=============================================================
{schema}

=============================================================
USER QUERY
=============================================================
{user_query}

=============================================================
OUTPUT FORMAT  (strict JSON — no markdown, no preamble)
=============================================================
{{
  "analysis_type": "comparison | trend | ranking | distribution | summary | lookup",
  "metrics": [
    {{
      "name":               "total_sales_value",
      "aggregation":        "SUM",
      "column":             "agreement_price",
      "derived_expression": "SUM(agreement_price)",
      "alias":              "total_sales_value"
    }},
    {{
      "name":               "units_sold",
      "aggregation":        "COUNT",
      "column":             "*",
      "derived_expression": "COUNT(*)",
      "alias":              "units_sold"
    }},
    {{
      "name":               "rate_per_sq_ft",
      "aggregation":        "DERIVED",
      "column":             "agreement_price / net_carpet_area_sq_m / 10.764",
      "derived_expression": "ROUND(SUM(agreement_price)::numeric / NULLIF(SUM(net_carpet_area_sq_m), 0) / 10.764, 2)",
      "alias":              "rate_per_sq_ft"
    }}
  ],
  "entities": {{
    "locations": [
      {{ "value": "Baner",     "semantic_level": "locality" }},
      {{ "value": "Hinjewadi", "semantic_level": "locality" }}
    ],
    "space_filters": {{
      "unit_number": null,
      "tower_name": null,
      "plot_number": null,
      "project_name": null,
      "location_name": null,
      "micro_market": null,
      "city_name": null,
      "state_name": null,
      "country_name": null
    }},
    "property_types": [],
    "projects":       [],
    "developers":     [],
    "limit":          null
  }},
  "filters": {{
    "transaction_category": "sale",
    "year":    null,
    "quarter": null,
    "extra":   []
  }},
  "group_by":   ["location_name"],
  "order_by":   [{{ "column": "total_sales_value", "direction": "DESC" }}],
  "time_series": false,
  "route": "internal_db",
  "needs_clarification": false,
  "clarification_reason": "",
  "clarification_questions": [],
  "raw_query":  "{user_query}"
}}
"""


# ══════════════════════════════════════════════════════════════════════════════
# Prompt: SQL Build
# ══════════════════════════════════════════════════════════════════════════════

SQL_BUILD_PROMPT = """
You are a PostgreSQL query generation agent for a real-estate intelligence platform.

You receive a structured intent object and the full database schema.
Generate ONE correct, efficient, executable PostgreSQL SELECT query.

=============================================================
NON-NEGOTIABLE RULES
=============================================================
1.  Schema is the only source of truth for column and table names.
    Never invent a column or table not in the schema.

2.  Intent is the only source of truth for WHAT to query.
    Include EVERY entity from entities.locations / entities.projects /
    entities.property_types / entities.space_filters in the WHERE clause.
    Never drop any entity. Never filter to just the first one.

3.  Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE,
    CREATE, GRANT, or REVOKE.

4.  Return only ONE valid PostgreSQL SELECT query.
    No markdown. No explanation. No comments. No semicolon at end is fine.

5.  STRICT FILTER RULE: Use `ILIKE` for ALL text/string comparisons (location_name, project_name, etc.). 
    NEVER use `=` for text columns. Use `%` wildcards generously: `location_name ILIKE '%Baner%'`.

=============================================================
ENTITY COMPLETENESS  (most critical SQL rule)
=============================================================
For comparison queries with multiple locations, use OR:

  (location_col ILIKE '%Baner%' OR location_col ILIKE '%Hinjewadi%')

  — 2 locations  → 2 OR branches
  — 5 locations  → 5 OR branches
  — Never AND across location values (that returns 0 rows always)

=============================================================
COLUMN SELECTION — SEMANTIC LEVEL MATCHING
=============================================================
Match the column to the entity's semantic_level field. However, be intelligent: location data in this database is often sparse or inconsistently labeled. 

- "locality"      → Try location_name, but if you suspect it might be in others, you can check sub_locality, micro_market, or village_name.
- "city"          → Try city_name, then location_name.
- "project"       → Try project_name, then tower_name.

Explicit entities.space_filters mapping:
- unit_number    → unit_number
- tower_name     → tower_name
- project_name   → project_name
- location_name  → location_name, sub_locality, village_name, and micro_market when useful
- micro_market   → micro_market
- city_name      → city_name
- state_name     → state_name
- country_name   → country_name
- plot_number    → property_description, because transaction schema has no dedicated plot/survey/CTS/khasra column

INTELLIGENT LOCATION SEARCH:
If a user specifies a location like "Baner", it could be in `location_name`, `sub_locality`, OR `micro_market`. To be robust, you may search across multiple candidates:
  (location_name ILIKE '%Baner%' OR sub_locality ILIKE '%Baner%' OR micro_market ILIKE '%Baner%')

Verify every chosen column exists in the schema before using it.

=============================================================
ANALYSIS TYPE RULES
=============================================================
  "comparison"   → GROUP BY entity column. ORDER BY primary metric DESC.
                   All entities via OR in WHERE.

  "trend"        → GROUP BY year, quarter. ORDER BY year ASC, quarter ASC.
                   SELECT time dims + metrics + COUNT(*).
                   Never return a single flat aggregate for a trend.

  "ranking"      → GROUP BY entity. ORDER BY metric DESC.
                   LIMIT if entities.limit is non-null.

  "distribution" → GROUP BY dimension. SELECT dimension + COUNT or SUM.

  "summary"      → Single aggregated result. No GROUP BY unless explicit.

  "lookup"       → SELECT specific columns. WHERE filters. LIMIT.

=============================================================
METRIC CONSTRUCTION
=============================================================
For EACH metric in intent.metrics:
  - Use derived_expression as the SQL expression verbatim.
  - Alias with the metric's alias field.
  - NULLIF around any denominator.
  - ROUND(...::numeric, 2) on any float metric.
  - Always add COUNT(*) AS transaction_count on every aggregated query.

rate_per_sq_ft formula (use exactly this):
  ROUND(
    SUM(agreement_price)::numeric
    / NULLIF(SUM(net_carpet_area_sq_m), 0)
    / 10.764,
    2
  ) AS rate_per_sq_ft

=============================================================
DATA QUALITY FILTERS  (always apply)
=============================================================
  agreement_price >= 1
  net_carpet_area_sq_m >= 1   (when area is in denominator)

=============================================================
SCHEMA
=============================================================
{schema}

=============================================================
INTENT
=============================================================
{intent_json}

=============================================================
PROBE RESULTS (Discovery of where the data lives)
=============================================================
{probe_results}

=============================================================
OUTPUT
=============================================================
Return only one valid PostgreSQL SELECT query. No markdown. No explanation.
"""


# ══════════════════════════════════════════════════════════════════════════════
# Prompt: SQL Review
# ══════════════════════════════════════════════════════════════════════════════

SQL_REVIEW_PROMPT = """
You are a senior PostgreSQL query reviewer for a real-estate intelligence platform.

Review the SQL query BEFORE execution. Your #1 job is entity completeness.

=============================================================
REVIEW CHECKLIST  (in priority order)
=============================================================
1.  ENTITY COMPLETENESS (reject immediately if this fails):
    Count locations in intent.entities.locations.
    Count OR branches in the WHERE clause filtering those locations.
    They must be equal.
    Example: intent has [Baner, Hinjewadi] → WHERE must have both.
    Missing even one entity = immediate rejection with corrected SQL.
    Also verify every non-empty entities.space_filters value appears in WHERE.

2.  Correct analysis_type:
    - comparison → GROUP BY entity col, ORDER BY metric DESC
    - trend      → GROUP BY year + quarter, ORDER BY time ASC

3.  Metric expressions:
    - NULLIF around denominators
    - ROUND for float metrics
    - rate_per_sq_ft uses SUM(ap)/SUM(area)/10.764

4.  ILIKE on all text filters.

5.  No phantom columns (not in schema).

6.  Semantic column match (locality → location_name preferred).

7.  GROUP BY consistent with SELECT.

8.  Data quality filters present.

9.  Risk of 0 rows due to overly strict filters.

=============================================================
SCHEMA
=============================================================
{schema}

=============================================================
INTENT
=============================================================
{intent_json}

=============================================================
SQL TO REVIEW
=============================================================
{sql}

=============================================================
OUTPUT FORMAT  (strict JSON — no markdown, no preamble)
=============================================================
{{
  "approved": true | false,
  "confidence": 0-100,
  "issues": ["issue1", "issue2"],
  "entity_completeness_check": {{
    "expected_entities": ["Baner", "Hinjewadi"],
    "found_in_sql": ["Baner", "Hinjewadi"],
    "missing": []
  }},
  "suggested_fix": "complete corrected SQL if approved=false, else null",
  "reasoning": "brief explanation"
}}
"""


# ══════════════════════════════════════════════════════════════════════════════
# Prompt: Probe (Location discovery)
# ══════════════════════════════════════════════════════════════════════════════

SQL_PROBE_PROMPT = """
You are a database discovery agent. 
Given a list of entities (locations, projects, and explicit space_filters), generate ONE PostgreSQL query to check which columns contain data for these entities.

The goal is to find WHERE the data lives. DO NOT add any other filters (like city, category, or date) that might accidentally kill the results. Just check the names.

STRICT RULE: Use `ILIKE` for all column checks. NEVER use `=`.
Example: `location_name ILIKE '%Baner%'`

Format:
For EACH entity, return a row showing counts for each candidate column.

Example for entity 'Baner':
SELECT 
  'Baner' AS entity,
  COUNT(*) FILTER (WHERE location_name ILIKE '%Baner%') AS in_location_name,
  COUNT(*) FILTER (WHERE sub_locality ILIKE '%Baner%') AS in_sub_locality,
  COUNT(*) FILTER (WHERE micro_market ILIKE '%Baner%') AS in_micro_market,
  COUNT(*) FILTER (WHERE village_name ILIKE '%Baner%') AS in_village_name
FROM transactions
WHERE (location_name ILIKE '%Baner%' OR sub_locality ILIKE '%Baner%' OR micro_market ILIKE '%Baner%' OR village_name ILIKE '%Baner%')

INTENT:
{intent_json}

Return only the SQL. No markdown. No extra filters.
"""


# ══════════════════════════════════════════════════════════════════════════════
# Prompt: Observe
# ══════════════════════════════════════════════════════════════════════════════

SQL_OBSERVE_PROMPT = """
You are a result quality analyst for a real-estate SQL agent.

Evaluate the query result against the user's intent.

=============================================================
VERDICT OPTIONS
=============================================================
  "good"        → Non-empty. ALL expected entities present in rows. Correct granularity.
  "empty"       → 0 rows returned.
  "db_error"    → PostgreSQL exception.
  "wrong_column"→ Non-empty but wrong column used for filter.
  "wrong_gran"  → Wrong granularity (e.g. trend → one row).
  "irrelevant"  → Results don't match intent (e.g. missing entities in output).

For comparison queries: check that ALL entities from intent.entities.locations
appear in the result rows. 
- If multiple locations are found: verdict = "good".
- If ONLY ONE location is found but it IS one of the requested ones: verdict = "good" (with a note in reason that the other was not found).
- If ZERO requested locations are found: verdict = "empty" or "irrelevant".
- Do NOT use "wrong_gran" if only one row is found because the other data simply isn't in the database.

=============================================================
INTENT
=============================================================
{intent_json}

=============================================================
SQL EXECUTED
=============================================================
{sql}

=============================================================
RESULT
=============================================================
Row count  : {row_count}
DB error   : {db_error}
Sample rows: {sample_rows}

=============================================================
OUTPUT FORMAT  (strict JSON — no markdown, no preamble)
=============================================================
{{
  "verdict": "<verdict>",
  "confidence": 0-100,
  "reason": "Technical reason for this verdict",
  "action_summary": "Short layman explanation of what you will do next (e.g. 'I'm searching in village names since micro market was empty.')",
  "missing_entities": ["intent entities not in result rows"],
  "column_suspect": "column likely wrong or null",
  "suggested_replacement_columns": ["col1", "col2"],
  "needs_broader_filter": true | false,
  "other_notes": "any other observations"
}}
"""


# ══════════════════════════════════════════════════════════════════════════════
# Prompt: Reflect
# ══════════════════════════════════════════════════════════════════════════════

SQL_REFLECT_PROMPT = """
You are a reflection agent for a real-estate SQL pipeline.

A SQL query produced an unsatisfactory result. Diagnose WHY and produce
a complete corrected SQL query.

=============================================================
REFLECTION RULES
=============================================================
1.  STRICT FILTER RULE: Use `ILIKE` for ALL text comparisons. NEVER use `=`.
2.  Schema is the only source of truth for column names.
    Never use a column not in the schema.

2.  Entity completeness is the #1 check.
    If missing_entities is non-empty, the corrected SQL MUST include
    ALL of them in the WHERE clause using OR:
      (location_col ILIKE '%A%' OR location_col ILIKE '%B%')

3.  Column fallback — reason from the filter VALUE's semantic level:

    Locality names (Baner, Wakad, Hinjewadi…)
      → Try order: location_name → sub_locality → micro_market → village_name

    City names (Pune, Mumbai…)
      → Try order: city_name → location_name

    Project / building names
      → Try order: project_name → tower_name → location_name

    Developer names
      → Try order: developer_name (if in schema) → project_name

    Property config (2BHK, 3 BHK…)
      → Try order: unit_configuration → property_type → property_type_raw

    Reason about semantic level first — do not apply a fixed order blindly.

4.  When unsure which column is right, use OR across candidates:
      (location_name ILIKE '%X%' OR sub_locality ILIKE '%X%')

5.  Check iteration history — never retry a column that already returned
    0 rows. columns_tried_in_filters lists every column tried per iteration.

6.  For DB errors: fix the exact error. Do not change intent.

7.  For wrong_gran on trend: add year + quarter to SELECT and GROUP BY.

8.  Always keep data quality filters:
      agreement_price >= 1
      net_carpet_area_sq_m >= 1 (when area is in denominator)

=============================================================
SCHEMA
=============================================================
{schema}

=============================================================
INTENT
=============================================================
{intent_json}

=============================================================
FAILED SQL
=============================================================
{sql}

=============================================================
OBSERVATION
=============================================================
{observation}

=============================================================
ITERATION HISTORY  (columns already tried — do not repeat)
=============================================================
{history}

=============================================================
SAMPLE ROWS FROM FAILED QUERY
=============================================================
{sample_rows}

=============================================================
REASONING STEPS  (work through before writing corrected_sql)
=============================================================
1. What is the verdict? What does it tell me concretely?
2. Are any intent entities missing from the result? Which ones?
3. Which column or filter is the culprit?
4. What semantic level does the filter value belong to?
5. Which schema column best matches that level and hasn't been tried?
6. What is the minimal fix? Does the corrected SQL include ALL entities?

=============================================================
OUTPUT FORMAT  (strict JSON — no markdown, no preamble)
=============================================================
{{
  "root_cause": "one-sentence diagnosis",
  "action": "fix_entity_completeness | replace_column | relax_filter | fix_aggregation | fix_groupby | fix_metric | broaden_with_or | other",
  "old_column": "column being replaced or null",
  "new_column": "replacement column from schema or null",
  "reasoning": "why this column semantically matches the filter value",
  "missing_entities_fix": ["entities being added to corrected SQL"],
  "corrected_sql": "complete corrected SQL — only SQL, no markdown",
  "explanation": "why this fix should work"
}}
"""


# ══════════════════════════════════════════════════════════════════════════════
# Prompt: Fix (DB error hard fallback)
# ══════════════════════════════════════════════════════════════════════════════

SQL_FIX_PROMPT = """
You are a PostgreSQL SQL correction agent.

Fix the failed SQL while preserving the original intent exactly.

Rules:
1.  Return only one corrected PostgreSQL SELECT query.
2.  Do not add unsupported columns or tables.
3.  Do not change intent unless required to fix the DB error.
4.  No DML/DDL statements.
5.  No markdown, no explanation.
6.  Use only columns from the provided schema.
7.  Add NULLIF for division safety if needed.
8.  Fix GROUP BY to match SELECT if needed.
9.  Preserve ALL entities from the original WHERE clause.

FAILED SQL:
{sql}

ERROR:
{error}

SCHEMA:
{schema}

Return only the corrected SQL.
"""


# ══════════════════════════════════════════════════════════════════════════════
# Intent Extractor
# ══════════════════════════════════════════════════════════════════════════════

def _extract_space_metadata_filters(text: str) -> dict[str, str]:
    """
    Parse the UI clarification metadata shape:
      selected_options=city
      additional_details=Pune

    This prevents a follow-up answer like "Pune" from looping back into the
    same clarification prompt when the selected space type is already known.
    """
    if not text:
        return {}

    selected: list[str] = []
    details = ""
    for raw_line in text.splitlines():
        key, sep, value = raw_line.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "selected_options":
            selected = [
                item.strip().lower()
                for item in re.split(r"[,|]", value)
                if item.strip()
            ]
        elif key in {"additional_details", "other_text"} and value:
            details = value

    if not selected or not details:
        return {}

    filters: dict[str, str] = {}
    for option in selected:
        field = SPACE_OPTION_TO_FIELD.get(option)
        if field:
            filters[field] = details
    return filters


def _infer_space_filters(user_query: str) -> dict[str, str]:
    regex_filters, _ = extract_space_filters(user_query, SPACE_FILTER_FIELD_ORDER)
    metadata_filters = _extract_space_metadata_filters(user_query)

    filters: dict[str, str] = {}
    filters.update(regex_filters)
    filters.update(metadata_filters)
    if "city" in filters and "city_name" not in filters:
        filters["city_name"] = filters.pop("city")
    return {k: v for k, v in filters.items() if v not in (None, "")}


def _merge_space_filters(intent: dict, user_query: str) -> None:
    entities = intent.get("entities")
    if not isinstance(entities, dict):
        entities = {}
        intent["entities"] = entities
    existing = entities.get("space_filters")
    if not isinstance(existing, dict):
        existing = {}

    inferred = _infer_space_filters(user_query)
    merged = {
        k: v for k, v in existing.items()
        if isinstance(k, str) and v not in (None, "")
    }
    for field, value in inferred.items():
        merged.setdefault(field, value)

    if merged:
        entities["space_filters"] = merged


def _contains_space_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_contains_space_value(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_space_value(item) for item in value)
    return value not in (None, "", [], {})


def _contains_named_entity(value: Any) -> bool:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if _contains_space_value(item.get("value") or item.get("name")):
                    return True
            elif _contains_space_value(item):
                return True
        return False
    if isinstance(value, dict):
        return _contains_space_value(value.get("value") or value.get("name"))
    return _contains_space_value(value)


def _intent_has_space_context(intent: dict) -> bool:
    entities = intent.get("entities") or {}
    if _contains_named_entity(entities.get("locations")):
        return True
    if _contains_named_entity(entities.get("projects")):
        return True
    if _contains_space_value(entities.get("space_filters")):
        return True

    # Be tolerant of older extractor outputs that put these fields elsewhere.
    legacy_filters = entities.get("filters") if isinstance(entities, dict) else None
    if _contains_space_value(legacy_filters):
        for field in SPACE_FILTER_FIELD_ORDER:
            if _contains_space_value((legacy_filters or {}).get(field)):
                return True

    extra_filters = (intent.get("filters") or {}).get("extra")
    if isinstance(extra_filters, list):
        for item in extra_filters:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or item.get("column") or "").lower()
            if field in SPACE_FILTER_FIELD_ORDER or field in SPACE_OPTION_TO_FIELD.values():
                if _contains_space_value(item.get("value")):
                    return True

    return False


def _mark_space_clarification_required(intent: dict) -> None:
    intent["route"] = "clarify"
    intent["needs_clarification"] = True
    intent["clarification_reason"] = (
        "I need to know which space or geography to filter before querying transaction data."
    )
    intent["clarification_questions"] = [SPACE_CLARIFICATION_QUESTION]


class IntentExtractor:
    """
    Converts a raw user query string into a structured intent dict.

    This is the ONLY pre-processing step before SQL generation.
    It faithfully captures everything the user asked for — all entities,
    all metrics, the analysis type — without narrowing or pre-filtering.
    """

    def __init__(self, client: OpenAI, model: str = "gpt-5.1") -> None:
        self.client = client
        self.model  = model
        self.last_usage = None

    def extract(self, user_query: str, history: list[dict] | None = None) -> dict:
        """
        Extract structured intent from raw user_query with conversation context.

        Returns a dict with analysis_type, metrics, entities, filters,
        group_by, order_by, and time_series fields.

        Raises ValueError if the LLM response cannot be parsed as JSON.
        """
        prompt = INTENT_EXTRACT_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
            user_query=user_query,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract structured query intent from natural language. "
                    "Use conversation history to resolve pronouns and follow-up requests. "
                    "Example: if user previously asked for 'Baner' and now says 'add total sales', "
                    "the intent should include both 'Baner' and 'total_sales_value'. "
                    "Respond only with valid JSON."
                ),
            },
        ]
        if history:
            # Add last 4 messages for context (2 user, 2 assistant)
            for msg in history[-4:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
        
        messages.append({"role": "user", "content": prompt})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            timeout=20,
        )
        self.last_usage = response.usage
        raw    = response.choices[0].message.content.strip()
        intent = _parse_json(raw, default=None)

        if intent is None:
            raise ValueError(
                f"IntentExtractor: failed to parse LLM response: {raw[:300]}"
            )

        _merge_space_filters(intent, user_query)
        if not _intent_has_space_context(intent):
            _mark_space_clarification_required(intent)

        locations = (intent.get("entities") or {}).get("locations") or []
        metrics   = [m.get("alias") for m in (intent.get("metrics") or [])]

        logger.info(
            "IntentExtractor: analysis_type=%s  locations=%s  metrics=%s",
            intent.get("analysis_type"),
            [loc.get("value") for loc in locations],
            metrics,
        )
        return intent


# ══════════════════════════════════════════════════════════════════════════════
# Transaction Query Builder  (ReAct loop)
# ══════════════════════════════════════════════════════════════════════════════

class TransactionQueryBuilder:
    """
    Production-grade ReAct SQL query builder.

    Takes a structured intent dict (from IntentExtractor) and runs a
    BUILD → REVIEW → EXECUTE → OBSERVE → REFLECT loop until a satisfactory
    result is produced or MAX_ITERATIONS is reached.

    No tool registry. No plan steps. No pre-filtering.
    All entities in the intent appear in every generated SQL.

    Usage
    ─────
        extractor = IntentExtractor(client)
        intent    = extractor.extract(user_query)

        builder   = TransactionQueryBuilder(client=client, db_executor=run_sql)
        result    = builder.run(intent)

        result.sql        → final SQL string
        result.rows       → result rows  (list[dict])
        result.intent     → extracted intent (for audit/logging)
        result.trace      → full ReAct trace  (list[Iteration])
        result.success    → True if GOOD verdict reached
    """

    def __init__(
        self,
        client:         OpenAI,
        db_executor:    Callable[[str], list[dict]] | None = None,
        model:          str = "gpt-5.1",
        max_iterations: int = MAX_ITERATIONS,
        **kwargs,
    ) -> None:
        self.client         = client
        self.db_executor    = db_executor
        self.model          = model
        self.max_iterations = max_iterations

        # Per-run state — reset on each run()
        self.trace:       list[Iteration] = []
        self.last_usage:  Any             = None
        self.total_usage: Any             = None
        self._fix_usages: list            = []

    # ══════════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════════

    def run(self, intent: dict) -> QueryResult:
        """
        Run the full ReAct loop and return a QueryResult.

        Loop:
          BUILD / REWRITE → REVIEW → EXECUTE → OBSERVE
                                                  │
                                             GOOD → return
                                                  │
                                             REFLECT → new SQL → next iteration
        """
        self.trace       = []
        self._fix_usages = []

        if intent.get("route") == "clarify" or intent.get("needs_clarification"):
            return QueryResult(
                sql="",
                rows=[],
                intent=intent,
                iterations=0,
                trace=[],
                success=False,
                error="Intent clarification required.",
            )

        # ── STEP 0: PROBE ─────────────────────────────────────────────────────
        # Identify which columns actually contain the entities to avoid empty results.
        probe_data = self._probe(intent)
        print(f"[ReAct] PROBE results: {probe_data}")

        current_sql: str | None = None
        last_rows:   list[dict] = []
        last_error:  str | None = None

        for i in range(self.max_iterations):
            iteration = Iteration(index=i, sql="", status=StepStatus.BUILD)
            t0 = time.monotonic()

            # ── STEP 1: BUILD / REWRITE ───────────────────────────────────────
            try:
                if current_sql is None:
                    print(f"\n[ReAct iter={i}] BUILD — generating initial SQL...")
                    logger.info("[iter=%d] BUILD — generating SQL from intent", i)
                    current_sql      = self._build(intent, probe_results=probe_data)
                    iteration.status = StepStatus.BUILD
                    print(f"SQL GENERATED:\n{current_sql}\n")
                else:
                    print(f"\n[ReAct iter={i}] REWRITE — applying correction...")
                    logger.info("[iter=%d] REWRITE — applying reflected SQL", i)
                    iteration.status = StepStatus.REWRITE
                
                iteration.usage = self.last_usage
                iteration.sql   = current_sql

            except Exception as exc:
                iteration.error  = str(exc)
                iteration.status = StepStatus.FAILED
                self.trace.append(iteration)
                logger.error("[iter=%d] BUILD failed: %s", i, exc)
                break

            # ── STEP 2: REVIEW ────────────────────────────────────────────────
            print(f"[ReAct iter={i}] REVIEW — checking entity completeness...")
            logger.info("[iter=%d] REVIEW — pre-execution gate", i)
            review           = self._review(intent, current_sql)
            iteration.review = review
            iteration.status = StepStatus.REVIEW

            if not review.get("approved", True):
                missing = (
                    review
                    .get("entity_completeness_check", {})
                    .get("missing", [])
                )
                print(f"[ReAct iter={i}] REVIEW rejected — missing entities: {missing}")
                logger.info(
                    "[iter=%d] REVIEW rejected  confidence=%s  missing=%s  reason=%s",
                    i,
                    review.get("confidence"),
                    missing,
                    review.get("reasoning"),
                )
                fix_sql = (review.get("suggested_fix") or "").strip()
                if fix_sql.lower().startswith(("select", "with")):
                    current_sql      = _clean_sql(fix_sql)
                    iteration.sql    = current_sql
                    print(f"[ReAct iter={i}] REVIEW — applied suggested fix.")
                    logger.info("[iter=%d] REVIEW — applying reviewer's corrected SQL", i)

            # ── STEP 3: EXECUTE ───────────────────────────────────────────────
            logger.info("[iter=%d] EXECUTE — %s", i, current_sql)
            rows, db_error = self._execute(current_sql)
            last_rows      = rows
            last_error     = db_error

            iteration.rows          = rows
            iteration.error         = db_error
            iteration.status        = StepStatus.EXECUTE
            iteration.columns_tried = _extract_filter_columns(current_sql)

            # ── STEP 4: OBSERVE ───────────────────────────────────────────────
            print(f"[ReAct iter={i}] OBSERVE — rows={len(rows)} error={db_error or 'none'}")
            logger.info(
                "[iter=%d] OBSERVE — rows=%d  error=%s",
                i, len(rows), db_error or "none",
            )
            observation       = self._observe(intent, current_sql, rows, db_error)
            
            # Normalize verdict for robustness
            raw_v = str(observation.get("verdict", "good")).lower().strip()
            if raw_v in ["good", "success", "correct", "perfect", "satisfactory"]:
                v_enum = ObserveVerdict.GOOD
            elif "empty" in raw_v:
                v_enum = ObserveVerdict.EMPTY
            elif "error" in raw_v:
                v_enum = ObserveVerdict.DB_ERROR
            else:
                try:
                    v_enum = ObserveVerdict(raw_v)
                except ValueError:
                    v_enum = ObserveVerdict.IRRELEVANT

            iteration.verdict = v_enum
            iteration.reflect = observation
            iteration.status  = StepStatus.OBSERVE
            iteration.duration_ms = int((time.monotonic() - t0) * 1000)
            self.trace.append(iteration)
            
            print(f"[ReAct iter={i}] VERDICT: {iteration.verdict.value}")
            print(f"REASON: {observation.get('reason')}")
            
            verdict = iteration.verdict

            # ── DONE ──────────────────────────────────────────────────────────
            if verdict == ObserveVerdict.GOOD:
                logger.info("[iter=%d] DONE — verdict=GOOD", i)
                return QueryResult(
                    sql=current_sql,
                    rows=rows,
                    intent=intent,
                    iterations=i + 1,
                    trace=self.trace,
                    success=True,
                    usage=self.total_usage,
                )

            # ── STEP 5: REFLECT → REWRITE ─────────────────────────────────────
            if i < self.max_iterations - 1:
                logger.info(
                    "[iter=%d] REFLECT — verdict=%s  missing=%s  suspect=%s",
                    i,
                    verdict,
                    observation.get("missing_entities", []),
                    observation.get("column_suspect"),
                )
                print(f"[ReAct iter={i}] REFLECT — diagnosing and correcting...")
                history    = self._build_history_summary()
                reflection = self._reflect(intent, current_sql, observation, history)
                
                if reflection.get("root_cause"):
                    print(f"REASONING: {reflection.get('root_cause')}")
                if reflection.get("explanation"):
                    print(f"EXPLANATION: {reflection.get('explanation')}")

                corrected = (reflection.get("corrected_sql") or "").strip()
                if corrected.lower().startswith(("select", "with")):
                    current_sql = _clean_sql(corrected)
                    print(f"CORRECTED SQL:\n{current_sql}\n")
                    print(f"[ReAct iter={i}] REWRITE — action: {reflection.get('action')}")
                    logger.info(
                        "[iter=%d] REWRITE — action=%s  missing_fix=%s  new_col=%s",
                        i,
                        reflection.get("action"),
                        reflection.get("missing_entities_fix", []),
                        reflection.get("new_column"),
                    )
                elif verdict == ObserveVerdict.DB_ERROR and db_error:
                    current_sql = self._fix_sql(current_sql, db_error)
                    print(f"[ReAct iter={i}] REWRITE — applied hard fallback fix.")
                    logger.info("[iter=%d] REWRITE — via hard fix() fallback", i)
                else:
                    logger.warning(
                        "[iter=%d] REFLECT produced no valid SQL — stopping early", i
                    )
                    break
            else:
                logger.warning(
                    "Max iterations (%d) reached without a satisfactory result",
                    self.max_iterations,
                )

        # ── Best-effort fallback ───────────────────────────────────────────────
        best = self._best_effort(last_rows)
        return QueryResult(
            sql=current_sql or "",
            rows=best,
            intent=intent,
            iterations=self.max_iterations,
            trace=self.trace,
            success=False,
            usage=self.total_usage,
            error=f"Max iterations ({self.max_iterations}) reached without GOOD verdict.",
        )

    # ── Backward compatibility ─────────────────────────────────────────────────

    def build(self, intent: dict, **kwargs) -> str:
        """Returns SQL string only — no ReAct loop."""
        if intent.get("route") == "clarify" or intent.get("needs_clarification"):
            return ""
        return self._build(intent)

    def fix(self, sql: str, error: str) -> str:
        """Fix a failed SQL string."""
        return self._fix_sql(sql, error)

    def pop_fix_usages(self) -> list:
        out = self._fix_usages[:]
        self._fix_usages = []
        return out

    # ══════════════════════════════════════════════════════════════════════════
    # Private — LLM calls
    # ══════════════════════════════════════════════════════════════════════════

    def _build(self, intent: dict, probe_results: str = "No probe data") -> str:
        prompt = SQL_BUILD_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
            intent_json=json.dumps(intent, indent=2),
            probe_results=probe_results,
        )
        response = self._chat(
            system=(
                "You generate safe, valid, schema-grounded PostgreSQL SELECT queries. "
                "Use the provided PROBE RESULTS to pick columns that actually contain data. "
                "Include EVERY entity from the intent in the WHERE clause using OR. "
                "Return only the SQL — no markdown, no explanation."
            ),
            user=prompt,
        )
        self.last_usage = response.usage
        raw = response.choices[0].message.content.strip()
        return _validate_select_only(_clean_sql(raw))

    def _probe(self, intent: dict) -> str:
        """Run a discovery query to find where entities reside."""
        locations = (intent.get("entities") or {}).get("locations") or []
        projects  = (intent.get("entities") or {}).get("projects") or []
        space_filters = (intent.get("entities") or {}).get("space_filters") or {}
        if not locations and not projects and not _contains_space_value(space_filters):
            return "No location/project entities to probe."

        prompt = SQL_PROBE_PROMPT.format(
            intent_json=json.dumps(intent, indent=2),
        )
        response = self._chat(
            system="You generate PostgreSQL discovery queries. Return only SQL.",
            user=prompt,
        )
        sql = _clean_sql(response.choices[0].message.content.strip())
        print(f"[ReAct] PROBE SQL: {sql}")
        
        try:
            rows = self.db_executor(sql)
            if not rows:
                return "Probe returned no rows."
            return json.dumps(rows, indent=2)
        except Exception as e:
            logger.warning("Probe failed: %s", e)
            return f"Probe failed: {e}"

    def _review(self, intent: dict, sql: str) -> dict:
        prompt = SQL_REVIEW_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
            intent_json=json.dumps(intent, indent=2),
            sql=sql,
        )
        response = self._chat(
            system=(
                "You review PostgreSQL queries for correctness and entity completeness. "
                "Respond only with JSON."
            ),
            user=prompt,
        )
        return _parse_json(
            response.choices[0].message.content,
            default={
                "approved": True,
                "confidence": 50,
                "issues": [],
                "entity_completeness_check": {},
                "reasoning": "parse error — defaulting approved",
            },
        )

    def _observe(
        self,
        intent:   dict,
        sql:      str,
        rows:     list[dict],
        db_error: str | None,
    ) -> dict:
        sample = rows[:REVIEW_SAMPLE] if rows else []
        prompt = SQL_OBSERVE_PROMPT.format(
            intent_json=json.dumps(intent, indent=2),
            sql=sql,
            row_count=len(rows),
            db_error=db_error or "none",
            sample_rows=json.dumps(sample, indent=2, default=str),
        )
        response = self._chat(
            system=(
                "You evaluate SQL query results for correctness, entity completeness, "
                "and intent alignment. Respond only with JSON."
            ),
            user=prompt,
        )
        return _parse_json(
            response.choices[0].message.content,
            default={
                "verdict": "good",
                "confidence": 50,
                "reason": "parse error — defaulting to good",
                "missing_entities": [],
            },
        )

    def _reflect(
        self,
        intent:      dict,
        sql:         str,
        observation: dict,
        history:     str,
    ) -> dict:
        sample_rows: list[dict] = []
        if self.trace:
            sample_rows = self.trace[-1].rows[:REVIEW_SAMPLE]

        prompt = SQL_REFLECT_PROMPT.format(
            schema=TRANSACTION_QUERY_SCHEMA,
            intent_json=json.dumps(intent, indent=2),
            sql=sql,
            observation=json.dumps(observation, indent=2),
            history=history,
            sample_rows=json.dumps(sample_rows, indent=2, default=str),
        )
        response = self._chat(
            system=(
                "You diagnose SQL failures and produce corrected SQL. "
                "Use schema as ground truth. Ensure ALL intent entities appear "
                "in the corrected WHERE clause. Respond only with JSON."
            ),
            user=prompt,
        )
        return _parse_json(response.choices[0].message.content, default={})

    def _fix_sql(self, sql: str, error: str) -> str:
        """Hard fallback for DB errors — single-purpose dedicated fixer."""
        prompt = SQL_FIX_PROMPT.format(
            sql=sql,
            error=error,
            schema=TRANSACTION_QUERY_SCHEMA,
        )
        response = self._chat(
            system="You fix PostgreSQL SELECT queries. Return only corrected SQL.",
            user=prompt,
        )
        self._fix_usages.append(response.usage)
        fixed = _clean_sql(response.choices[0].message.content.strip())
        return _validate_select_only(fixed)

    # ══════════════════════════════════════════════════════════════════════════
    # Private — execution
    # ══════════════════════════════════════════════════════════════════════════

    def _execute(self, sql: str) -> tuple[list[dict], str | None]:
        """
        Run SQL via the injected db_executor.
        Never raises — exceptions become the error string.
        """
        try:
            rows = self.db_executor(sql)
            return rows or [], None
        except Exception as exc:
            logger.warning("DB execution error: %s", exc)
            return [], str(exc)

    # ══════════════════════════════════════════════════════════════════════════
    # Private — helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _chat(self, system: str, user: str):
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            timeout=30,
        )
        self.last_usage = response.usage
        self._update_total_usage(response.usage)
        return response

    def _update_total_usage(self, usage: Any):
        if usage is None: return
        if self.total_usage is None:
            # We can't easily instantiate a CompletionUsage, so we'll just 
            # keep it as a dict or a simple object for summing.
            self.total_usage = {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens
            }
        else:
            self.total_usage["prompt_tokens"]     += usage.prompt_tokens
            self.total_usage["completion_tokens"] += usage.completion_tokens
            self.total_usage["total_tokens"]      += usage.total_tokens

    def _build_history_summary(self) -> str:
        """
        Compact history for the reflection prompt.
        Surfaces columns_tried_in_filters so the LLM avoids retrying
        columns that already returned 0 rows.
        """
        summary = []
        for it in self.trace:
            summary.append({
                "iter":                     it.index,
                "sql_snippet":              it.sql[:400] + ("…" if len(it.sql) > 400 else ""),
                "verdict":                  it.verdict,
                "error":                    it.error,
                "row_count":                len(it.rows),
                "columns_tried_in_filters": it.columns_tried,
                "missing_entities":         it.reflect.get("missing_entities", []),
                "observe_reason":           it.reflect.get("reason"),
                "column_suspect":           it.reflect.get("column_suspect"),
                "suggested_replacements":   it.reflect.get("suggested_replacement_columns"),
            })
        return json.dumps(summary, indent=2, default=str)

    def _best_effort(self, last_rows: list[dict]) -> list[dict]:
        """Return last non-empty result from trace after exhausting iterations."""
        for it in reversed(self.trace):
            if it.rows:
                return it.rows
        return last_rows


# ══════════════════════════════════════════════════════════════════════════════
# Top-level convenience function
# ══════════════════════════════════════════════════════════════════════════════

def run_query(
    user_query:  str,
    client:      OpenAI,
    db_executor: Callable[[str], list[dict]],
    model:       str = "gpt-5.1",
) -> QueryResult:
    """
    Single entry point: raw user query string → QueryResult.

    Pipeline:
      1. IntentExtractor converts the query to a structured intent dict.
      2. TransactionQueryBuilder runs the ReAct loop against the intent.

    Example
    ───────
        result = run_query(
            user_query  = "total sales units sold and rate per sqft "
                          "compare baner and hinjewadi",
            client      = openai_client,
            db_executor = run_sql,
        )
        print(result.sql)
        print(result.rows)
        print(result.success)
    """
    extractor = IntentExtractor(client=client, model=model)
    intent    = extractor.extract(user_query)

    builder   = TransactionQueryBuilder(
        client=client,
        db_executor=db_executor,
        model=model,
    )
    return builder.run(intent)


# ══════════════════════════════════════════════════════════════════════════════
# Module-level pure helpers
# ══════════════════════════════════════════════════════════════════════════════

def _clean_sql(sql: str) -> str:
    """Strip markdown fences that LLMs occasionally emit."""
    sql = sql.strip()
    sql = re.sub(r"^```sql\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"^```\s*",    "", sql)
    sql = re.sub(r"\s*```$",    "", sql)
    return sql.strip()


def _validate_select_only(sql: str) -> str:
    """
    Raise ValueError if the SQL is not a SELECT/WITH query or contains
    any blocked DML/DDL keyword. Returns unchanged SQL if valid.
    """
    sql_lower = sql.strip().lower()
    blocked = re.compile(
        r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke)\b",
        re.IGNORECASE,
    )
    if not (sql_lower.startswith("select") or sql_lower.startswith("with")):
        raise ValueError(
            f"Generated SQL is not a SELECT/WITH query. Got: {sql[:80]}"
        )
    if blocked.search(sql_lower):
        raise ValueError("Generated SQL contains a blocked DML/DDL keyword.")
    return sql


def _parse_json(text: str, default: Any) -> Any:
    """
    Robustly parse JSON from an LLM response.
    Strips markdown fences, tries full parse, then extracts first {...}.
    Returns default on all failures.
    """
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```$",        "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    logger.warning(
        "_parse_json: could not parse LLM response — using default. Preview: %s",
        text[:200],
    )
    return default


def _extract_filter_columns(sql: str) -> list[str]:
    """
    Extract column names from WHERE-clause filter expressions.

    Used to build the history summary so the reflector knows which columns
    have already been tried and should not be retried.

    Patterns matched:
      col ILIKE '%val%'
      col = 'val'
      col IN (...)
      col BETWEEN x AND y
      col IS [NOT] NULL
      col >= / <= / > / < number
    """
    patterns = [
        r"\b(\w+)\s+ILIKE\s+",
        r"\b(\w+)\s*=\s*'",
        r"\b(\w+)\s+IN\s+\(",
        r"\b(\w+)\s+BETWEEN\s+",
        r"\b(\w+)\s+IS\s+(?:NOT\s+)?NULL",
        r"\b(\w+)\s*[><=!]+\s*\d",
    ]
    cols: list[str] = []
    for pattern in patterns:
        cols.extend(re.findall(pattern, sql, re.IGNORECASE))

    sql_keywords = {
        "where", "and", "or", "not", "on", "join", "having",
        "case", "when", "then", "else", "end", "select", "from",
        "null", "true", "false",
    }
    return list({c.lower() for c in cols if c.lower() not in sql_keywords})
