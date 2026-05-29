# Transaction Agent - Stage 1.5 Metric Verification

## Overview

Stage 1.5 is an intermediate verification step between Stage 1 (Intent Extraction) and Stage 2 (Algorithm Creation). Its primary purpose is to:

1. **Verify** that all metrics mentioned in the user query are captured in Stage 1 output
2. **Identify** any missing metrics
3. **Complete** the metrics list by adding missing ones
4. **Combine** `OUTPUT_JSON_SCHEMA` and `MAPPED_JSON_SCHEMA` into a single `FINAL_JSON_SCHEMA`
5. **Pass** the verified output to Stage 2 for algorithm generation

## Workflow

```
User Query
    ↓
[Stage 1: Intent Extraction]
    ↓
    OUTPUT: OUTPUT_JSON_SCHEMA + MAPPED_JSON_SCHEMA
    ↓
[Stage 1.5: Metric Verification] ← NEW
    ↓
    OUTPUT: FINAL_JSON_SCHEMA (with complete metrics list)
    ↓
[Stage 2: Algorithm Creation]
    ↓
    OUTPUT: Algorithm with verified metrics
```

## Usage

### Option 1: Run Complete Workflow (Recommended)

```bash
# Run all stages in sequence
python -m agents.data_retrieval_transaction.complete_workflow \
    "Show total sales value and transaction count in Baner for 2024"
```

### Option 2: Run Stage 1.5 Standalone

```bash
# First run Stage 1
python -m agents.data_retrieval_transaction.stage1_sample \
    "Show total sales value and transaction count in Baner for 2024" > stage1_output.json

# Then run Stage 1.5 with the output
python -m agents.data_retrieval_transaction.stage1_5_metric_verification \
    "Show total sales value and transaction count in Baner for 2024" \
    --stage1-output stage1_output.json
```

### Option 3: Python API

```python
from openai import OpenAI
from agents.data_retrieval_transaction.stage1_sample import TransactionStage1SampleAgent
from agents.data_retrieval_transaction.stage1_5_metric_verification import TransactionStage1_5SampleAgent
from agents.data_retrieval_transaction.complete_workflow import run_complete_workflow

# Option A: Use complete workflow
result = run_complete_workflow(
    user_query="Show total sales and transaction count in Mumbai"
)
final_verified_intent = result["final_verified_intent"]

# Option B: Manual pipeline
client = OpenAI(api_key="your-key")

# Stage 1
stage1_agent = TransactionStage1SampleAgent(client)
stage1_events = list(stage1_agent.execute_stage1_events("your query"))
stage1_output = [e for e in stage1_events if e["type"] == "intent"][0]["content"]

# Stage 1.5
stage1_5_agent = TransactionStage1_5SampleAgent(client)
stage1_5_events = list(stage1_5_agent.execute_stage1_5_events("your query", stage1_output))
final_verified_intent = [e for e in stage1_5_events if e["type"] == "verified_intent"][0]["content"]
```

## Input Format

### Stage 1.5 Input (from Stage 1 output)

```json
{
  "OUTPUT_JSON_SCHEMA": {
    "analysis_type": "summary",
    "intent": "Get total sales and transaction count",
    "metrics": [
      {"name": "total_sales_value", "alias": "sales"}
    ],
    "entities": {
      "locations": [{"value": "Baner"}]
    }
  },
  "MAPPED_JSON_SCHEMA": {
    "analysis_type": "summary",
    "intent": "Get total sales and transaction count",
    "metrics": [...],
    "entities": {
      "space_field": "location_name",
      "property_type": "residential",
      "time_period": "2024"
    }
  }
}
```

## Output Format

### Stage 1.5 Output

```json
{
  "stage": "1.5",
  "verification_status": "complete",
  "verification_complete": true,
  "user_query_metrics": [
    {
      "name": "total_sales_value",
      "description": "Total sales/agreement price",
      "source": "user_query"
    },
    {
      "name": "transaction_count",
      "description": "Count of transactions",
      "source": "user_query"
    }
  ],
  "stage1_metrics": [
    {
      "name": "total_sales_value",
      "alias": "sales"
    }
  ],
  "missing_metrics": [
    {
      "name": "transaction_count",
      "alias": "transaction_count",
      "type": "count",
      "description": "Total number of transactions",
      "reason_for_addition": "Explicitly mentioned in user query but missing from Stage 1 output"
    }
  ],
  "added_metrics_count": 1,
  "metrics_verification_summary": "Verification complete. Found 1 missing metric (transaction_count) and added it to the list.",
  "FINAL_JSON_SCHEMA": {
    "analysis_type": "summary",
    "intent": "Get total sales and transaction count in Baner for 2024",
    "metrics": [
      {
        "name": "total_sales_value",
        "alias": "sales",
        "type": "sum",
        "description": "Total agreement/deal value"
      },
      {
        "name": "transaction_count",
        "alias": "transaction_count",
        "type": "count",
        "description": "Total number of transactions"
      }
    ],
    "entities": {
      "space_field": "location_name",
      "property_type": "residential",
      "time_period": "2024",
      "transaction_category": "sale"
    },
    "expected_output": "Summary showing total sales value and transaction count for Baner in 2024",
    "verification_complete": true,
    "needs_clarification": false
  }
}
```

## Key Features

### 1. Metric Extraction from User Query

Stage 1.5 identifies all metrics mentioned (explicitly or implicitly) in the user query:

- **Explicit mentions**: "show total sales", "calculate average price"
- **Implicit mentions**: "sales performance" (implies sales volume, value, trends)
- **Common metrics**: count, sum, average, min, max, rate, percentage, trend

### 2. Metric Comparison

Compares extracted metrics against Stage 1 output:

- Checks for semantic equivalence (e.g., "total transactions" = "transaction_count")
- Identifies missing metrics
- Flags metrics that need consolidation

### 3. Metric Completion

Adds missing metrics with proper structure:

```json
{
  "name": "metric_name",
  "alias": "short_alias",
  "type": "aggregation_type",
  "description": "what this metric represents"
}
```

### 4. Schema Combination

Merges OUTPUT_JSON_SCHEMA and MAPPED_JSON_SCHEMA into FINAL_JSON_SCHEMA:

- Uses mapped values where available
- Inherits all entity and filter information
- Adds verification metadata
- Creates clean output for Stage 2

## Common Metrics Recognized

The Stage 1.5 system recognizes the following metric patterns:

### Count Metrics
- "transaction count", "number of transactions", "units sold", "property count"
- Type: `count`

### Sum Metrics
- "total sales", "total value", "total agreement price", "total guideline value"
- Type: `sum`

### Average Metrics
- "average price", "mean value", "average price per sqm"
- Type: `avg`

### Rate Metrics
- "price per sqm", "sales rate", "absorption rate", "rate of growth"
- Type: `rate`

### Trend Metrics
- "quarterover-quarter", "month-on-month", "year-over-year change"
- Type: `trend`

## Configuration

### Environment Variables

```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_ADMIN_KEY="fallback-api-key"  # Optional fallback
```

### Model Selection

```bash
# Use different models for different stages
python -m agents.data_retrieval_transaction.complete_workflow \
    "Your query" \
    --stage1-model gpt-4 \
    --stage1-5-model gpt-4o-mini \
    --stage2-model gpt-4o-mini
```

## Integration with Existing Codebase

### From Live Transaction Agent

To integrate Stage 1.5 into the live transaction agent pipeline:

```python
# In your agent's execute() method:
from agents.data_retrieval_transaction.stage1_5_metric_verification import TransactionStage1_5SampleAgent

# After Stage 1 completes:
stage1_5_agent = TransactionStage1_5SampleAgent(client)
stage1_5_events = stage1_5_agent.execute_stage1_5_events(user_query, stage1_output)

# Extract the verified intent:
for event in stage1_5_events:
    if event["type"] == "verified_intent":
        verified_intent = event["content"]
        # Pass to Stage 2 instead of raw Stage 1 output
        break
```

## Troubleshooting

### Issue: Missing metrics are not being added

**Solution**: Ensure the user query explicitly mentions the metrics. Stage 1.5 uses the user query to identify metrics - implicit or unclear references may not be captured.

**Example**:
```
Bad: "Show me insights about Baner"
Good: "Show me total sales value and transaction count in Baner"
```

### Issue: Stage 1.5 adds duplicate metrics

**Solution**: This may indicate a semantic mismatch. Stage 1.5 performs semantic comparison, but complex metric names may need clarification.

**Fix**: Review `FINAL_JSON_SCHEMA` metrics and manually remove duplicates if needed.

### Issue: FINAL_JSON_SCHEMA has incomplete entities

**Solution**: Ensure Stage 1 passed complete entity information. If Stage 1 needed clarification, Stage 1.5 will also require it.

## Next Steps

After Stage 1.5, the `FINAL_JSON_SCHEMA` is ready for:

1. **Stage 2**: Algorithm creation for metric calculation
2. **SQL Generation**: Direct SQL query building (if your pipeline supports it)
3. **Data Retrieval**: Query execution and result formatting

## Performance Notes

- Stage 1.5 introduces one additional LLM call
- Token usage typically 200-400 tokens
- Processing time: < 5 seconds for typical queries
- Overhead: ~5% additional API cost vs. direct Stage 1→2 flow

## Future Enhancements

- [ ] Metric dependency analysis (metrics that require other metrics)
- [ ] Metric validation against available database columns
- [ ] Automatic metric formula generation
- [ ] Metric aggregation level detection (entity, regional, national)
