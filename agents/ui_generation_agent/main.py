import json
import re
from typing import Any

from langgraph.prebuilt import create_react_agent

from agents.shared.parsers import (
    collect_agent_trace,
    extract_jsx_from_payload,
    extract_text_from_content,
    try_parse_payload,
)
from agents.shared.usage import normalize_usage_metadata
from agents.ui_generation_agent.prompts import get_ui_generation_prompt
from agents.ui_generation_agent.tools import (
    generate_bar_chart_widget,
    generate_dashboard_html,
    generate_line_chart_widget,
    generate_metric_widget,
    generate_pie_chart_widget,
    generate_scatter_plot_widget,
    generate_table_widget,
)


def _get_ui_tools():
    return [
        generate_metric_widget,
        generate_table_widget,
        generate_bar_chart_widget,
        generate_line_chart_widget,
        generate_pie_chart_widget,
        generate_scatter_plot_widget,
        generate_dashboard_html,
    ]


def _build_data_profile(real_data_json: str) -> dict:
    data = try_parse_payload(real_data_json)
    if isinstance(data, dict) and all(isinstance(v, list) for v in data.values()):
        first_key = next(iter(data), None)
        rows = data[first_key] if first_key else []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    if not rows:
        return {
            "row_count": 0,
            "columns": [],
            "numeric_columns": [],
            "categorical_columns": [],
            "sample_rows": [],
            "notes": ["No usable tabular rows found."],
        }

    columns = list(rows[0].keys())
    numeric_columns = []
    categorical_columns = []
    for col in columns:
        values = [row.get(col) for row in rows if isinstance(row, dict)]
        non_null = [v for v in values if v is not None]
        if non_null and all(isinstance(v, (int, float)) for v in non_null):
            numeric_columns.append(col)
        else:
            categorical_columns.append(col)

    return {
        "row_count": len(rows),
        "columns": columns,
        "numeric_columns": numeric_columns,
        "categorical_columns": categorical_columns,
        "sample_rows": rows[:5],
        "dataset_count": len(data) if isinstance(data, dict) else 1,
    }


def _build_dataset_constants(real_data_json: str, missing_names: set[str]) -> str:
    parsed_data = try_parse_payload(real_data_json)
    datasets: dict[str, Any] = {}
    if isinstance(parsed_data, dict):
        for key, value in parsed_data.items():
            if re.fullmatch(r"dataset_\d+", str(key)):
                datasets[str(key)] = value if isinstance(value, list) else []
    elif isinstance(parsed_data, list):
        datasets["dataset_0"] = parsed_data

    lines = []
    for name in sorted(missing_names):
        value = datasets.get(name, [])
        lines.append(f"  const {name} = {json.dumps(value, default=str)};")
    return "\n".join(lines)


def _inject_missing_dataset_constants(jsx_output: str, real_data_json: str) -> str:
    referenced = set(re.findall(r"\bdataset_\d+\b", jsx_output))
    declared = set(re.findall(r"\b(?:const|let|var)\s+(dataset_\d+)\b", jsx_output))
    missing = referenced - declared
    if not missing:
        return jsx_output

    declarations = _build_dataset_constants(real_data_json, missing)
    if not declarations:
        return jsx_output

    patterns = [
        r"(export\s+default\s+function\s+[^{]*\{\s*)",
        r"(function\s+[A-Za-z0-9_]+\s*\([^)]*\)\s*\{\s*)",
        r"(const\s+[A-Za-z0-9_]+\s*=\s*\([^)]*\)\s*=>\s*\{\s*)",
    ]
    for pattern in patterns:
        updated, count = re.subn(
            pattern,
            lambda match: match.group(1) + "\n" + declarations + "\n",
            jsx_output,
            count=1,
            flags=re.DOTALL,
        )
        if count:
            return updated
    return declarations + "\n" + jsx_output


def _validate_generated_jsx(jsx_output: str) -> tuple[bool, list[str]]:
    issues = []
    jsx_lower = jsx_output.lower()
    if "return" not in jsx_lower and "export" not in jsx_lower:
        issues.append("No return or export statement found")
    if "```" in jsx_output:
        issues.append("Markdown fence leaked into JSX")
    if jsx_output.strip().startswith("```"):
        issues.append("JSX wrapped in markdown fence")
    return len(issues) == 0, issues


def _extract_jsx_from_ui_result(result: dict) -> str:
    for msg in reversed(result["messages"]):
        payload_text = extract_text_from_content(getattr(msg, "content", ""))
        jsx_candidate = extract_jsx_from_payload(payload_text)
        if jsx_candidate:
            return jsx_candidate.strip()

    for msg in reversed(result["messages"]):
        if getattr(msg, "name", "") in {
            "generate_dashboard_html",
            "generate_metric_widget",
            "generate_table_widget",
            "generate_bar_chart_widget",
            "generate_line_chart_widget",
            "generate_pie_chart_widget",
            "generate_scatter_plot_widget",
        }:
            payload_text = extract_text_from_content(getattr(msg, "content", ""))
            jsx_candidate = extract_jsx_from_payload(payload_text)
            if jsx_candidate:
                return jsx_candidate.strip()
    return ""


def run_ui_generation_agent(
    llm,
    user_query: str,
    grounded_schema_json: str,
    component_count: int,
    real_data_json: str,
) -> tuple[str, dict[str, Any] | None]:
    react_agent = create_react_agent(
        llm,
        tools=_get_ui_tools(),
        prompt=get_ui_generation_prompt(),
    )
    data_profile = _build_data_profile(real_data_json)
    user_message = f"""
User Intent:
{user_query}

Grounded UI Schema:
{grounded_schema_json}

Planned Component Count:
{component_count}

Real Data Source of Truth:
The following JSON contains the actual data returned from the database.
You must use this data for all calculations, summaries, chart values, labels, and tables.

Data Profile:
{json.dumps(data_profile, indent=2)}

Real Data JSON:
{real_data_json}

Build the final UI now using your tools.
"""
    result = react_agent.invoke({"messages": [("user", user_message)]})
    jsx_output = _extract_jsx_from_ui_result(result)
    jsx_issues: list[str] = []

    if not jsx_output:
        jsx_issues.append("UI agent failed to generate final JSX.")
    else:
        parsed_final = try_parse_payload(jsx_output)
        if parsed_final is not None:
            unwrapped_jsx = extract_jsx_from_payload(parsed_final)
            if unwrapped_jsx:
                jsx_output = unwrapped_jsx.strip()

        jsx_output = _inject_missing_dataset_constants(jsx_output, real_data_json)
        is_valid_jsx, validation_issues = _validate_generated_jsx(jsx_output)
        if not is_valid_jsx:
            jsx_issues.extend(validation_issues)

    if jsx_issues:
        agent_trace = collect_agent_trace(result["messages"])
        raise RuntimeError(
            "UI agent failed. "
            f"Issues: {jsx_issues}. "
            f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
        )

    usage = None
    for msg in reversed(result["messages"]):
        usage = normalize_usage_metadata(msg)
        if usage:
            break

    return jsx_output, usage
