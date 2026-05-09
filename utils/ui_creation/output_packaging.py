import json


def build_final_result(
    jsx_output: str,
    semantic_schema_dict: dict,
    grounded_schema_dict: dict,
    semantic_mapping: dict,
    real_data_json: str,
) -> dict:
    return {
        "jsx": jsx_output,
        "html": jsx_output,
        "schema_plan": grounded_schema_dict,
        "semantic_schema_plan": semantic_schema_dict,
        "semantic_mapping": semantic_mapping,
        "insights": [
            {
                "title": "Fully Agentic Semantic-to-Grounded Pipeline",
                "description": "Agent 1 generated semantic schema from the query only. Agent 2 resolved it to the best relevant real DB columns and generated SQL.",
                "type": "success",
            }
        ],
        "data_summary": (
            real_data_json[:1200] + "... (truncated)"
            if len(real_data_json) > 1200
            else real_data_json
        ),
    }


def build_unanswerable_jsx(user_query: str, reason: str) -> str:
    title_json = json.dumps("I cannot answer this from the available data")
    query_json = json.dumps(user_query)
    reason_json = json.dumps(reason)
    return f"""export default function GeneratedAnswer() {{
  const title = {title_json};
  const query = {query_json};
  const reason = {reason_json};

  return (
    <section style={{{{
      fontFamily: "Inter, system-ui, sans-serif",
      maxWidth: "760px",
      margin: "32px auto",
      padding: "24px",
      border: "1px solid #d8dee8",
      borderRadius: "8px",
      background: "#ffffff",
      color: "#162033"
    }}}}>
      <p style={{{{ margin: "0 0 8px", fontSize: "13px", color: "#667085" }}}}>Request</p>
      <h2 style={{{{ margin: "0 0 16px", fontSize: "24px", lineHeight: 1.25 }}}}>{{query}}</h2>
      <h3 style={{{{ margin: "0 0 10px", fontSize: "18px" }}}}>{{title}}</h3>
      <p style={{{{ margin: 0, fontSize: "15px", lineHeight: 1.6, color: "#344054" }}}}>{{reason}}</p>
    </section>
  );
}}"""


def build_unanswerable_final_result(
    user_query: str,
    reason: str,
    semantic_schema_dict: dict,
    resolver_result: dict,
) -> dict:
    jsx_output = build_unanswerable_jsx(user_query, reason)
    return {
        "jsx": jsx_output,
        "html": jsx_output,
        "schema_plan": resolver_result.get("grounded_schema", {}),
        "semantic_schema_plan": semantic_schema_dict,
        "semantic_mapping": resolver_result.get("semantic_mapping", {}),
        "insights": [
            {
                "title": "No matching data source",
                "description": reason,
                "type": "warning",
            }
        ],
        "data_summary": reason,
    }
