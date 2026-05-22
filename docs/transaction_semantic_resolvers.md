# Transaction Semantic Resolvers

These two helper modules improve transaction-agent SQL accuracy before SQL is generated.

They run inside `TransactionQueryBuilder.run()` before the probe/build/review/execute loop:

```text
intent extraction
-> semantic category resolution
-> semantic project/location/city resolution
-> SQL probe
-> SQL build
-> SQL review
-> SQL execution
```

## `semantic_different_categories.py`

Purpose: converts user category words into exact database values.

Example:

```text
User says: 2BHK residential sales
DB stores: unit_configuration = "2 BHK", property_type = "Residential"
```

The resolver adds exact matches to the intent:

```json
{
  "semantic_category_queries": {
    "unit_configuration": ["2BHK"]
  },
  "semantic_resolved_filters": {
    "unit_configuration": ["2 BHK"]
  }
}
```

### Columns It Resolves

For the `transactions` table, it resolves category-like columns:

- `transaction_category`
- `property_type`
- `unit_configuration`
- `project_type`
- `sale_type`
- `furnishing_status`
- `condition_status`
- `facing_direction`
- `view_type`
- `bank_type`

For the `projects` table, it resolves:

- `project_type`

### Main Functions

`collect_intent_category_queries(intent)`

Collects raw user category values from the extracted intent, such as `2BHK`, `residential`, or `semi furnished`.

`get_distinct_values(table_name, columns, db_executor, ...)`

Fetches real distinct values from the database for the relevant columns.

`resolve_query(query_value, distinct_values, client, model)`

Matches one raw user phrase against real DB values. It uses:

- LLM semantic matching for synonyms, abbreviations, and spelling variants.
- Case-insensitive substring matching for partial/composite values.

`resolve_intent_category_filters(intent, client, db_executor, ...)`

Main function used by the agent. It collects raw category terms, fetches DB values, resolves them, and writes the result back into:

```python
intent["semantic_resolved_filters"]
```

### Safety Helpers

`_ensure_safe_table` and `_ensure_safe_columns` allow only approved tables and columns.

`_parse_json` safely parses LLM JSON output.

`_clean_values` removes empty and duplicate DB values.

## `semantic_project_location_name.py`

Purpose: converts user-entered project, location, and city names into exact database spellings.

Example:

```text
User says: bombey, vtp bellesimo
DB stores: Mumbai, VTP Bellissimo
```

The resolver updates the intent in-place and records what changed:

```json
{
  "semantic_resolved_entities": {
    "cities": [
      {
        "path": "entities.space_filters.city_name",
        "original": "bombey",
        "resolved": "Mumbai"
      }
    ]
  }
}
```

### Entity Groups

For the `transactions` table:

- `projects`: `project_name`
- `locations`: `location_name`, `sub_locality`, `micro_market`, `village_name`
- `cities`: `city_name`

For the `projects` table:

- `projects`: `project_name`, `registered_project_name`
- `locations`: `location_name`, `sub_locality`, `micro_market`
- `cities`: `city_name`

### Main Functions

`load_entity_cache(table_name, db_executor, ...)`

Loads distinct project/location/city values from the database and caches them per table.

`resolve_entity(value, category, table_name, db_executor, ...)`

Resolves one value to the exact DB spelling. Matching order:

1. Normalize text.
2. Apply city aliases like `bombay -> mumbai`, `poona -> pune`.
3. Try exact lowercase match.
4. Try compressed match without spaces, hyphens, or underscores.
5. Try fuzzy match with `difflib`.
6. Return the original value if no match is found.

`_resolve_text_with_fallbacks(value, categories, ...)`

Tries nearby categories when the intent extractor placed an entity in the wrong bucket.

Example: if `VTP Bellissimo` is detected as a location, it can still resolve as a project.

`_resolve_named_entity_list(items, category, ...)`

Resolves lists like:

```python
entities["locations"]
entities["projects"]
```

`resolve_intent_space_entities(intent, table_name, db_executor)`

Main function used by the agent. It resolves:

- `entities.locations`
- `entities.projects`
- `entities.space_filters`

It also moves misclassified project names from `locations` into `projects` when needed.

### Supporting Helpers

`_normalize`

Lowercases text, removes noise words like `project`, `location`, `city`, and applies city aliases.

`_compress`

Removes spaces, hyphens, and underscores for flexible matching.

`_build_category_cache`

Creates exact, fuzzy, and compressed lookup maps from DB values.

`SPACE_FIELD_TO_CATEGORY`

Maps intent space filter fields to resolver categories, such as:

```text
project_name -> projects
location_name -> locations
city_name -> cities
```

## Combined Effect

Together, these modules make the SQL builder work with exact database values instead of rough user text.

```text
User query
-> intent extractor creates raw intent
-> category resolver fixes category-like filters
-> project/location resolver fixes names and spellings
-> SQL builder receives cleaner intent
-> fewer empty or wrong SQL results
```

Short example:

```text
User: Show 2BHK sales in bombey for vtp bellesimo

semantic_different_categories:
2BHK -> 2 BHK

semantic_project_location_name:
bombey -> Mumbai
vtp bellesimo -> VTP Bellissimo
```

Then the SQL builder can create filters using real database values.
