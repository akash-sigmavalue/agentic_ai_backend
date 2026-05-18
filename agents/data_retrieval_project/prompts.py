from __future__ import annotations

"""
Project Query Builder Prompts
=============================
Prompt templates used by project intent extraction and SQL generation.
"""

# Prompt: Intent Extraction
# ══════════════════════════════════════════════════════════════════════════════

INTENT_EXTRACT_PROMPT = """
You are an intent extraction agent for a real-estate intelligence platform, focusing on the PROJECT domain.

Convert the user's natural language query into a structured JSON intent object.
This intent drives SQL generation — it must capture EVERYTHING the user asked for.

=============================================================
EXTRACTION RULES
=============================================================
0.  Planning rule:
    First analyze the user's query against the provided schema. Identify the
    requested intent, metrics, entities, filters, groupings, ordering, and any
    ambiguity before filling the JSON. Use only schema-backed columns and
    concepts. This planning must guide the structured intent.

1.  Capture EVERY entity the user mentions without exception.
    If the user says "compare Baner and Hinjewadi", both must appear
    in entities.locations. Never silently drop any entity.
    If the user mentions a unit, building/tower, parcel/survey/CTS/khasra/plot
    number, project, location/locality, micromarket, city, state, or country,
    capture it in entities.space_filters using the closest matching project
    schema field.

2.  Infer analysis_type:
    - "compare X and Y / X vs Y"   → "comparison"
    - "trend / over time / by year" → "trend"
    - "top N / rank / best"         → "ranking"
    - "how many / breakdown / split"→ "distribution"
    - "total / average / overall"   → "summary"
    - "show / list / find"          → "lookup"

3.  Infer ALL metrics the user asked for (Project specific):
    - "total units"                → SUM(total_units)   alias total_units
    - "available units"            → SUM(total_units - booked_units)   alias available_units
    - "booking rate / booked %"    → AVG(booked_units::numeric / NULLIF(total_units, 0) * 100) alias booking_rate
    - "projects / count"           → COUNT(*)           alias project_count
    - "plot area sqft"             → AVG(total_plot_area_sq_m * 10.764) alias plot_area_sqft
    Multiple metrics → list all of them.

4.  For each location entity, infer semantic_level:
    - Neighbourhood/locality (Baner, Wakad, Hinjewadi, Kothrud…) → "locality"
    - City (Pune, Mumbai, Nagpur…)                                → "city"
    - Project / building name                                      → "project"
    - Developer / builder name                                     → "developer"
    - Property configuration (2BHK, 3BHK, Studio…)                → "property_type"

5.  Infer standard filters:
    - Year / quarter if explicitly mentioned, else null.
    - Project category-like filters must also be captured in entities.category_filters:
      project_type
      Examples:
      "residential / commercial" → project_type

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
      "name":               "booking_rate",
      "aggregation":        "DERIVED",
      "column":             "booked_units / total_units",
      "derived_expression": "ROUND(AVG(booked_units::numeric / NULLIF(total_units, 0)) * 100, 2)",
      "alias":              "booking_rate"
    }},
    {{
      "name":               "project_count",
      "aggregation":        "COUNT",
      "column":             "*",
      "derived_expression": "COUNT(*)",
      "alias":              "project_count"
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
    "category_filters": {{
      "project_type": "residential",
      "construction_status": "under construction",
      "building_grade": null,
      "zoning_type": null,
      "encumbrance_status": null,
      "approval_status": null,
      "data_source": null,
      "source_accessibility": null,
      "source_accessibility_way": null,
      "data_type": null
    }},
    "property_types": [],
    "projects":       [],
    "developers":     [],
    "limit":          null
  }},
  "filters": {{
    "construction_status": "under construction",
    "year":    null,
    "quarter": null,
    "extra":   []
  }},
  "group_by":   ["location_name"],
  "order_by":   [{{ "column": "project_count", "direction": "DESC" }}],
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
You are a PostgreSQL query generation agent for a real-estate project intelligence platform.

You receive a structured intent object and the full database schema.
Generate ONE correct, efficient, executable PostgreSQL SELECT query.

=============================================================
NON-NEGOTIABLE RULES
=============================================================
0.  Planning rule:
    Before writing SQL, create an internal step-by-step algorithm:
    a. Interpret the user intent from the structured intent.
    b. Select only schema-backed tables and columns needed for the query.
    c. Map each entity/filter to the best matching schema column.
    d. Decide metric expressions, grouping, ordering, limits, and data-quality
       filters.
    e. Verify entity completeness and column validity.
    Then follow that algorithm exactly when generating the SQL. Do not output
    the algorithm in this stage; return only the SQL as required below.

1.  Schema is the only source of truth for column and table names.
    Never invent a column or table not in the schema.

2.  Intent is the only source of truth for WHAT to query.
    Include EVERY entity from entities.locations / entities.projects /
    entities.property_types / entities.developers / entities.space_filters /
    entities.category_filters / semantic_resolved_filters in the WHERE clause.
    Never drop any entity. Never filter to just the first one.

3.  Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE,
    CREATE, GRANT, or REVOKE.

4.  Return only ONE valid PostgreSQL SELECT query.
    No markdown. No explanation. No comments. No semicolon at end is fine.

5.  STRICT FILTER RULE: Use `ILIKE` for ALL text/string comparisons (location_name, project_name, organization_individual_name, etc.). 
    NEVER use `=` for text columns. Use `%` wildcards generously: `location_name ILIKE '%Baner%'`.
    Exception: values inside intent.semantic_resolved_filters are exact database
    values returned by semantic matching. Use those with `IN (...)` on the same
    column, and do not replace them with guessed spellings.

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

- "locality"      → Try location_name, but if you suspect it might be in others, you can check sub_locality, micro_market.
- "city"          → Try city_name, then location_name.
- "project"       → Try project_name, then registered_project_name.
- "developer"     → Try organization_individual_name.

Explicit entities.space_filters mapping:
- unit_number    → project_bhk_summary, bhk_wise_total_booked_units, or remark (project schema has no unit_number column)
- tower_name     → building_wise_total_booked_units, project_tower_completion_date, or remark (project schema has no tower_name column)
- plot_number    → plot_number
- project_name   → project_name, registered_project_name
- location_name  → location_name, sub_locality, and micro_market when useful
- micro_market   → micro_market
- city_name      → city_name
- state_name     → state_name
- country_name   → country_name

INTELLIGENT LOCATION SEARCH:
If a user specifies a location like "Baner", it could be in `location_name`, `sub_locality`, OR `micro_market`. To be robust, you may search across multiple candidates:
  (location_name ILIKE '%Baner%' OR sub_locality ILIKE '%Baner%' OR micro_market ILIKE '%Baner%')

Verify every chosen column exists in the schema before using it. Prefer projects as the base/anchor table.

=============================================================
PROJECT / LOCATION RESPONSE COLUMNS  (always include)
=============================================================
If the question is about any project or location, always return the matching
name column and coordinates in the SELECT output.

- Project-level query or entities.projects present:
  SELECT project_name, project_latitude, project_longitude

- Location/locality/city query or entities.locations / location space_filters present:
  SELECT location_name, location_latitude, location_longitude

- If the filter uses registered_project_name, still include project_name,
  project_latitude, and project_longitude when those columns exist.

- If the filter uses sub_locality, micro_market, or city_name, still include
  location_name, location_latitude, and location_longitude when those columns
  exist in the schema.

- For aggregate, comparison, trend, ranking, or distribution queries, every
  non-aggregated returned name/coordinate column must also appear in GROUP BY.

- For trend queries involving projects or locations, keep the time dimension,
  but also include the relevant project/location name and coordinates.

=============================================================
SEMANTIC RESOLVED FILTERS  (exact DB values)
=============================================================
If intent.semantic_resolved_filters contains any values, they are authoritative.
Add every project category column and every value to the WHERE clause.

Example:
  "semantic_resolved_filters": {{
    "project_type": ["Residential"],
  }}

Use:
  project_type IN ('Residential')

Do not use raw category_filters instead when semantic_resolved_filters has
values for that same column.

=============================================================
ANALYSIS TYPE RULES
=============================================================
  "comparison"   → GROUP BY entity column. ORDER BY primary metric DESC.
                   All entities via OR in WHERE.

  "trend"        → GROUP BY year, quarter (using commencement_date or final_proposed_date_of_completion). 
                   ORDER BY time ASC.
                   Never return a single flat aggregate for a trend.

  "ranking"      → GROUP BY entity. ORDER BY metric DESC.
                   LIMIT if entities.limit is non-null. (default LIMIT 30).

  "distribution" → GROUP BY dimension. SELECT dimension + COUNT or SUM.

  "summary"      → Single aggregated result. No GROUP BY unless explicit.

  "lookup"       → SELECT specific columns. WHERE filters. LIMIT 30.

=============================================================
METRIC CONSTRUCTION
=============================================================
For EACH metric in intent.metrics:
  - Use derived_expression as the SQL expression verbatim.
  - Alias with the metric's alias field.
  - NULLIF around any denominator.
  - ROUND(...::numeric, 2) on any float metric.
  - Always add COUNT(*) AS project_count on every aggregated query (if not already present).

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
You are a senior PostgreSQL query reviewer for a real-estate project intelligence platform.

Review the SQL query BEFORE execution. Your #1 job is entity completeness.

=============================================================
REVIEW CHECKLIST  (in priority order)
=============================================================
1.  ENTITY COMPLETENESS (reject immediately if this fails):
    Count locations/projects/developers in intent.entities.
    Count OR branches in the WHERE clause filtering those entities.
    They must be equal.
    Missing even one entity = immediate rejection with corrected SQL.
    Also verify every non-empty entities.space_filters value appears in WHERE.
    Also verify every column/value in intent.semantic_resolved_filters appears
    in WHERE. These values are exact DB values and should be used with IN (...).

2.  Correct analysis_type:
    - comparison → GROUP BY entity col, ORDER BY metric DESC
    - trend      → GROUP BY year/quarter, ORDER BY time ASC

3.  Metric expressions:
    - NULLIF around denominators
    - ROUND for float metrics

4.  ILIKE on all text filters, except intent.semantic_resolved_filters values,
    which are exact DB values and may use IN (...).

5.  No phantom columns (not in schema).

6.  Project/location response columns:
    If the question is about projects or locations, the SELECT must include the
    relevant name plus latitude and longitude:
    project_name + project_latitude + project_longitude for projects;
    location_name + location_latitude + location_longitude for locations.
    In aggregate queries these non-aggregated columns must also be in GROUP BY.

7.  Semantic column match (developer → organization_individual_name).

8.  GROUP BY consistent with SELECT.

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
# Prompt: Probe (Location/Entity discovery)
# ══════════════════════════════════════════════════════════════════════════════

SQL_PROBE_PROMPT = """
You are a database discovery agent. 
Given the user's intent (which contains entities like locations/projects/developers, explicit space_filters, AND text filters like project status), generate ONE PostgreSQL query to check which columns contain data for EACH text value.

The goal is to find WHERE the data lives (for both entities and filters). DO NOT add any other restrictions.

STRICT RULE: Use `ILIKE` for all column checks. NEVER use `=`.
Example: `location_name ILIKE '%Baner%'`

Format:
For EACH distinct entity or text filter value, return a row showing counts for each candidate column you think it might belong to.
Use UNION ALL if checking multiple values.

Example for entity 'Baner' and filter 'Under Construction':
SELECT 
  'Baner' AS search_term,
  COUNT(*) FILTER (WHERE location_name ILIKE '%Baner%') AS in_location_name,
  COUNT(*) FILTER (WHERE sub_locality ILIKE '%Baner%') AS in_sub_locality,
  0 AS in_construction_status
FROM projects
WHERE (location_name ILIKE '%Baner%' OR sub_locality ILIKE '%Baner%')
UNION ALL
SELECT 
  'Under Construction' AS search_term,
  0 AS in_location_name,
  0 AS in_sub_locality,
  COUNT(*) FILTER (WHERE construction_status ILIKE '%Under Construction%') AS in_construction_status
FROM projects
WHERE (construction_status ILIKE '%Under Construction%')

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

For comparison queries: check that ALL entities from intent.entities
appear in the result rows. 
- If multiple entities are found: verdict = "good".
- If ONLY ONE entity is found but it IS one of the requested ones: verdict = "good" (with a note in reason that the other was not found).
- If ZERO requested entities are found: verdict = "empty" or "irrelevant".

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
You are a reflection agent for a real-estate project SQL pipeline.

A SQL query produced an unsatisfactory result. Diagnose WHY and produce
a complete corrected SQL query.

=============================================================
REFLECTION RULES
=============================================================
1.  STRICT FILTER RULE: Use `ILIKE` for ALL text comparisons. NEVER use `=`.
    Exception: intent.semantic_resolved_filters contains exact DB values from
    semantic matching, so those values may be used with IN (...).
2.  Schema is the only source of truth for column names.
    Never use a column not in the schema.

3.  Entity completeness is the #1 check.
    If missing_entities is non-empty, the corrected SQL MUST include
    ALL of them in the WHERE clause using OR:
      (location_col ILIKE '%A%' OR location_col ILIKE '%B%')
    Also preserve every column/value in intent.semantic_resolved_filters.
    These are exact database values from semantic matching; use them with
    IN (...) and do not replace them with guessed variants.

    If the query is about projects or locations, preserve/add the relevant
    output identity columns and coordinates:
      project_name, project_latitude, project_longitude
      location_name, location_latitude, location_longitude
    Add these columns to GROUP BY whenever the corrected SQL aggregates.

4.  Column fallback — reason from the filter VALUE's semantic level:

    Locality names (Baner, Wakad)
      → Try order: location_name → sub_locality → micro_market

    City names (Pune, Mumbai…)
      → Try order: city_name → location_name

    Project / building names
      → Try order: project_name → registered_project_name → location_name

    Developer names
      → Try order: organization_individual_name → project_name

    Project Status (Under Construction, Completed, etc.)
      → Try order: construction_status → remark → approval_status
      → Or check if `final_proposed_date_of_completion > CURRENT_DATE` for 'Under Construction', or `< CURRENT_DATE` for 'Completed'.

5.  If a query returns 0 rows (empty verdict), question ALL filters. 
    It might be the location column, OR it might be the status/property type column. 
    Broaden or change columns for ANY filter that might be too restrictive.
    When unsure which column is right, use OR across candidates:
      (location_name ILIKE '%X%' OR sub_locality ILIKE '%X%')

6.  Check iteration history — never retry a column that already returned
    0 rows. columns_tried_in_filters lists every column tried per iteration.

7.  For DB errors: fix the exact error. Do not change intent.

8.  For wrong_gran on trend: add year + quarter to SELECT and GROUP BY.

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

