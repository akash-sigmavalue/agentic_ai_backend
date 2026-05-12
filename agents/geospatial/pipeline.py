from __future__ import annotations

# import json
# from sqlalchemy import text
# from langgraph.prebuilt import create_react_agent
# from langchain_core.prompts import ChatPromptTemplate

# from app.core.llm import get_llm
# from app.schemas.crm import PlannerSchemaOut, DBResolverPlanOut
# from app.db.database import SessionLocal
# from app.agents.tools import (
#     create_metric_widget_tool, 
#     create_bar_chart_tool, 
#     create_line_chart_tool,
#     create_table_tool,
#     assemble_crm_tool
# )

# def _execute_sql(sql_query: str) -> str:
#     """Helper function to execute SQL against the DB securely in Agent 2"""
#     try:
#         db = SessionLocal()
#         result = db.execute(text(sql_query))
#         rows = result.mappings().all()
#         db.close()
#         data = [dict(row) for row in rows]
#         return json.dumps(data, indent=2, default=str)
#     except Exception as e:
#         return f"Error executing query: {str(e)}"

# def execute_crm_pipeline(user_query: str) -> dict:
#     llm = get_llm()
    
#     # =========================================================
#     # AGENT 1: UI SCHEMA PLANNER
#     # =========================================================
#     planner_prompt = ChatPromptTemplate.from_messages([
#         ("system", "You are the Senior UI Planner for a Crm or Dashboard. The context is an 'employees' database table with: id, name, department, performance_score, attendance_pct, attrition_score, salary."),
#         ("human", "User Request: {query}\nProvide a structured JSON schema detailing what UI metrics, charts, or tables are needed to answer this request. If they ask for just a single chart, output a schema reflecting only that chart.")
#     ])
#     planner_chain = planner_prompt | llm.with_structured_output(PlannerSchemaOut)
#     schema_plan = planner_chain.invoke({"query": user_query})
    
#     # =========================================================
#     # AGENT 2: DATABASE RESOLVER
#     # =========================================================
#     resolver_prompt = ChatPromptTemplate.from_messages([
#         ("system", "You are a Database Architect. Generate the exact PostgreSQL query needed from the 'employees' table. The 'employees' table has EXACTLY these columns: id, name, department, performance_score, attendance_pct, attrition_score, salary. Do not use any other column names!"),
#         ("human", "UI Schema Requirement:\n{schema}\n\nReturn the valid SQL query.")
#     ])
#     resolver_chain = resolver_prompt | llm.with_structured_output(DBResolverPlanOut)
#     db_plan = resolver_chain.invoke({"schema": schema_plan.json()})
    
#     real_data_json = _execute_sql(db_plan.sql_query)
#     if real_data_json.startswith("Error"):
#         raise Exception(f"Failed to pull data for Agent 3: {real_data_json}")

#     # =========================================================
#     # AGENT 3: FRONTEND UI ReAct GENERATOR
#     # =========================================================
#     ui_tools = [create_metric_widget_tool, create_bar_chart_tool, create_line_chart_tool, create_table_tool, assemble_crm_tool]
    
#     system_prompt = """You are an Autonomous AI UI React Agent.
    
#     You have specialized UI generating tools. Your MUST use them to build the exact UI components requested in the schema, using the real data.
    
#     1. Review the Data and Schema.
#     2. Choose the correct tools: 
#        - Use `create_table_tool` for tabular data.
#        - Use `create_bar_chart_tool` / `create_line_chart_tool` for segmented aggregations.
#        - Use `create_metric_widget_tool` for top-level stats.
#     3. Compile the generated component HTML strings.
#     4. Crucially, ALWAYS call `assemble_crm_tool` passing in the compiled HTML strings to generate the final layout.
#        - Set layout_type="FULL_CRM" if the user implied they want a dashboard.
#        - Set layout_type="STANDALONE" if the user specifically asked for *just* a chart or a table.
    
#     Your final conversational response MUST primarily be the raw output from `assemble_crm_tool`. Do not include markdown wraps.
#     """
    
#     react_agent = create_react_agent(llm, tools=ui_tools, prompt=system_prompt)
    
#     user_message = f"User Intent: {user_query}\n\nUI Schema Context: {schema_plan.json()}\n\nReal Data:\n{real_data_json}\n\nBuild the UI using your tools!"
    
#     # Execute Final Agent
#     result = react_agent.invoke({"messages": [("user", user_message)]})
    
#     # Bulletproof extraction: Just look for the actual HTML payload anywhere in the response chain
#     html_output = ""
#     for msg in reversed(result["messages"]):
#         content_str = str(msg.content)
#         if "<!DOCTYPE html>" in content_str or "<html" in content_str.lower():
#             html_output = content_str
#             break
            
#     # Fallback if no HTML tag was found but a tool was ran
#     if not html_output:
#         for msg in reversed(result["messages"]):
#             if getattr(msg, "name", "") == "assemble_crm_tool":
#                 html_output = str(msg.content)
#                 break
                
#     # Final fallback
#     if not html_output:
#         html_output = str(result["messages"][-1].content)
    
#     if "```html" in html_output:
#         html_output = html_output.split("```html")[1].rsplit("```", 1)[0].strip()
        
#     return {
#         "html": html_output,
#         "schema_plan": schema_plan.dict(),
#         "insights": [{"title": "Dynamic Content", "description": "Layout configured automatically by ReAct Agent", "type": "success"}],
#         "data_summary": real_data_json[:500] + "... (truncated)" if len(real_data_json) > 500 else real_data_json
#     }
# import ast
# import json
# import re
# from typing import Any

# from sqlalchemy import text
# from langgraph.prebuilt import create_react_agent
# from langchain_core.prompts import ChatPromptTemplate

# from app.core.llm import get_llm
# from app.schemas.crm import PlannerSchemaOut, DBResolverPlanOut
# from app.db.database import SessionLocal
# from app.agents.tools import (
#     generate_crm_metric,
#     generate_crm_table,
#     generate_crm_bar_chart,
#     generate_crm_line_chart,
#     generate_crm_pie_chart,
#     generate_crm_scatter_plot,
#     generate_dashboard_html,
# )


# def _to_schema_json(schema_obj: Any) -> str:
#     if hasattr(schema_obj, "model_dump_json"):
#         return schema_obj.model_dump_json(indent=2)
#     if hasattr(schema_obj, "json"):
#         return schema_obj.json(indent=2)
#     return json.dumps(schema_obj, indent=2, default=str)


# def _to_schema_dict(schema_obj: Any) -> dict:
#     if hasattr(schema_obj, "model_dump"):
#         return schema_obj.model_dump()
#     if hasattr(schema_obj, "dict"):
#         return schema_obj.dict()
#     return dict(schema_obj)


# def _execute_sql(sql_query: str) -> str:
#     db = SessionLocal()
#     try:
#         result = db.execute(text(sql_query))
#         rows = result.mappings().all()
#         data = [dict(row) for row in rows]
#         return json.dumps(data, indent=2, default=str)
#     finally:
#         db.close()


# def _extract_text_from_content(content: Any) -> str:
#     if content is None:
#         return ""

#     if isinstance(content, str):
#         return content

#     if isinstance(content, dict):
#         return json.dumps(content, default=str)

#     if isinstance(content, list):
#         parts = []
#         for item in content:
#             if isinstance(item, dict):
#                 if "text" in item:
#                     parts.append(str(item["text"]))
#                 elif "content" in item:
#                     parts.append(str(item["content"]))
#                 else:
#                     parts.append(json.dumps(item, default=str))
#             else:
#                 parts.append(str(item))
#         return "\n".join(parts)

#     return str(content)


# def _try_parse_payload(text_value: str):
#     if not text_value:
#         return None

#     raw = text_value.strip()

#     try:
#         return json.loads(raw)
#     except Exception:
#         pass

#     try:
#         return ast.literal_eval(raw)
#     except Exception:
#         pass

#     return None


# def _extract_html_from_payload(payload: Any) -> str:
#     if payload is None:
#         return ""

#     if isinstance(payload, dict):
#         html_value = payload.get("html")
#         if isinstance(html_value, str) and html_value.strip():
#             return html_value.strip()

#         for _, value in payload.items():
#             found = _extract_html_from_payload(value)
#             if found:
#                 return found
#         return ""

#     if isinstance(payload, list):
#         for item in payload:
#             found = _extract_html_from_payload(item)
#             if found:
#                 return found
#         return ""

#     if isinstance(payload, str):
#         text_value = payload.strip()

#         code_match = re.search(r"```html\s*(.*?)```", text_value, flags=re.IGNORECASE | re.DOTALL)
#         if code_match:
#             candidate = code_match.group(1).strip()
#             if candidate:
#                 return candidate

#         if "<!DOCTYPE html>" in text_value or "<html" in text_value.lower():
#             return text_value

#         parsed = _try_parse_payload(text_value)
#         if parsed is not None:
#             return _extract_html_from_payload(parsed)

#         return ""

#     return ""


# def _collect_agent_trace(messages: list[Any]) -> list[dict]:
#     trace = []
#     for i, msg in enumerate(messages):
#         trace.append(
#             {
#                 "index": i,
#                 "type": type(msg).__name__,
#                 "name": getattr(msg, "name", None),
#                 "content_preview": _extract_text_from_content(getattr(msg, "content", ""))[:1200],
#             }
#         )
#     return trace


# def execute_crm_pipeline(user_query: str) -> dict:
#     llm = get_llm()

#     # =========================
#     # AGENT 1: UI SCHEMA PLANNER
#     # =========================
#     planner_prompt = ChatPromptTemplate.from_messages([
#         (
#             "system",
#             "You are the Senior UI Planner for a CRM and employment management dashboard. "
#             "The available source is an 'employees' table with EXACT columns: "
#             "id, name, department, performance_score, attendance_pct, attrition_score, salary."
#         ),
#         (
#             "human",
#             "User Request: {query}\n"
#             "Return a structured schema for the UI response. "
#             "Include title, required_metrics, charts, and tables. "
#             "If the user asks for only one widget, keep the schema narrow."
#         ),
#     ])
#     planner_chain = planner_prompt | llm.with_structured_output(PlannerSchemaOut)
#     schema_plan = planner_chain.invoke({"query": user_query})
#     schema_json = _to_schema_json(schema_plan)
#     schema_dict = _to_schema_dict(schema_plan)

#     # Count intended widgets to guide Agent 3
#     widget_count = (
#         len(schema_dict.get("required_metrics", []))
#         + len(schema_dict.get("charts", []))
#         + len(schema_dict.get("tables", []))
#     )

#     # =========================
#     # AGENT 2: DATABASE RESOLVER
#     # =========================
#     resolver_prompt = ChatPromptTemplate.from_messages([
#         (
#             "system",
#             "You are a PostgreSQL query planner. "
#             "Generate ONE valid SQL query using ONLY the 'employees' table and ONLY these columns: "
#             "id, name, department, performance_score, attendance_pct, attrition_score, salary. "
#             "Do not invent any column. Do not use any other table. "
#             "Prefer returning all rows needed for the requested UI."
#         ),
#         (
#             "human",
#             "UI Schema Requirement:\n{schema}\n\n"
#             "Return the SQL required to fetch the data needed for this response."
#         ),
#     ])
#     resolver_chain = resolver_prompt | llm.with_structured_output(DBResolverPlanOut)
#     db_plan = resolver_chain.invoke({"schema": schema_json})

#     real_data_json = _execute_sql(db_plan.sql_query)

#     # =========================
#     # AGENT 3: DYNAMIC UI AGENT
#     # =========================
#     ui_tools = [
#         generate_crm_metric,
#         generate_crm_table,
#         generate_crm_bar_chart,
#         generate_crm_line_chart,
#         generate_crm_pie_chart,
#         generate_crm_scatter_plot,
#         generate_dashboard_html,
#     ]

#     system_prompt = """
# You are an Autonomous UI Agent.

# Your job is to generate FINAL HTML yourself.
# The frontend will only render the final HTML returned by your tool output.

# STRICT RULES
# 1. Do not return raw JSON.
# 2. Do not return markdown code fences unless they are intentionally inside HTML content.
# 3. Do not ask the frontend to assemble anything.
# 4. Use inline CSS only.
# 5. Prefer pure HTML/CSS and inline SVG for charts.
# 6. You must always end with a tool output that already contains a complete HTML document.
# 7. For single-widget requests, call exactly one widget tool with standalone=True and stop there.
# 8. For multi-widget requests, call widget tools with standalone=False, combine their widget HTML, and then call generate_dashboard_html.
# 9. Do not call generate_dashboard_html for single-widget requests.
# 10. The final valid answer is only the tool payload containing an html field.

# TOOL RULES
# - Use generate_crm_metric for KPI cards and summary numbers.
# - Use generate_crm_table for detailed records and structured tables.
# - Use generate_crm_bar_chart for category comparisons.
# - Use generate_crm_line_chart for trend-like views.
# - Use generate_crm_pie_chart for distribution views.
# - Use generate_crm_scatter_plot for relationship views.
# - Use generate_dashboard_html only when there are multiple widgets in the final response.

# HTML QUALITY RULES
# - Build a professional business-friendly UI.
# - Keep spacing clean and readable.
# - Make tables scroll safely on smaller screens.
# - Do not output placeholders.
# - Do not output explanations outside the final tool chain.
# """

#     react_agent = create_react_agent(
#         llm,
#         tools=ui_tools,
#         prompt=system_prompt,
#     )

#     user_message = f"""
# User Intent:
# {user_query}

# Planned UI Schema:
# {schema_json}

# Planned Widget Count:
# {widget_count}

# Decision Rule:
# - If widget count is 1 or less, return exactly one standalone widget page.
# - If widget count is more than 1, return one combined dashboard page.

# Real Data:
# {real_data_json}

# Build the final UI now using your tools.
# """

#     result = react_agent.invoke({"messages": [("user", user_message)]})

#     html_output = ""

#     # Pass 1: inspect all messages for payload["html"] or direct HTML
#     for msg in reversed(result["messages"]):
#         payload_text = _extract_text_from_content(getattr(msg, "content", ""))
#         html_candidate = _extract_html_from_payload(payload_text)
#         if html_candidate:
#             html_output = html_candidate.strip()
#             break

#     # Pass 2: inspect relevant tool messages directly
#     if not html_output:
#         for msg in reversed(result["messages"]):
#             if getattr(msg, "name", "") in {
#                 "generate_dashboard_html",
#                 "generate_crm_metric",
#                 "generate_crm_table",
#                 "generate_crm_bar_chart",
#                 "generate_crm_line_chart",
#                 "generate_crm_pie_chart",
#                 "generate_crm_scatter_plot",
#             }:
#                 payload_text = _extract_text_from_content(getattr(msg, "content", ""))
#                 html_candidate = _extract_html_from_payload(payload_text)
#                 if html_candidate:
#                     html_output = html_candidate.strip()
#                     break

#     if not html_output:
#         agent_trace = _collect_agent_trace(result["messages"])
#         raise RuntimeError(
#             "UI agent failed to generate final HTML. "
#             f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
#         )

#     if "<html" not in html_output.lower():
#         agent_trace = _collect_agent_trace(result["messages"])
#         raise RuntimeError(
#             "UI agent returned content, but it is not a full HTML document. "
#             f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
#         )

#     return {
#         "html": html_output,
#         "schema_plan": schema_dict,
#         "insights": [
#             {
#                 "title": "Dynamic Content",
#                 "description": "UI generated dynamically through tool-based HTML rendering.",
#                 "type": "success",
#             }
#         ],
#         "data_summary": real_data_json[:1000] + "... (truncated)" if len(real_data_json) > 1000 else real_data_json,
#     }

# import ast
# import json
# import re
# from typing import Any

# from sqlalchemy import text
# from langgraph.prebuilt import create_react_agent
# from langchain_core.prompts import ChatPromptTemplate

# from app.core.llm import get_llm
# from app.schemas.crm import PlannerSchemaOut, DBResolverPlanOut
# from app.db.database import SessionLocal
# from app.agents.tools import (
#     generate_crm_metric,
#     generate_crm_table,
#     generate_crm_bar_chart,
#     generate_crm_line_chart,
#     generate_crm_pie_chart,
#     generate_crm_scatter_plot,
#     generate_dashboard_html,
# )


# ALLOWED_COLUMNS = [
#     "id",
#     "name",
#     "department",
#     "performance_score",
#     "attendance_pct",
#     "attrition_score",
#     "salary",
# ]


# def _to_schema_dict(schema_obj: Any) -> dict:
#     if hasattr(schema_obj, "model_dump"):
#         return schema_obj.model_dump()
#     if hasattr(schema_obj, "dict"):
#         return schema_obj.dict()
#     return dict(schema_obj)


# def _execute_sql(sql_query: str) -> str:
#     db = SessionLocal()
#     try:
#         result = db.execute(text(sql_query))
#         rows = result.mappings().all()
#         data = [dict(row) for row in rows]
#         return json.dumps(data, indent=2, default=str)
#     finally:
#         db.close()


# def _extract_text_from_content(content: Any) -> str:
#     if content is None:
#         return ""

#     if isinstance(content, str):
#         return content

#     if isinstance(content, dict):
#         return json.dumps(content, default=str)

#     if isinstance(content, list):
#         parts = []
#         for item in content:
#             if isinstance(item, dict):
#                 if "text" in item:
#                     parts.append(str(item["text"]))
#                 elif "content" in item:
#                     parts.append(str(item["content"]))
#                 else:
#                     parts.append(json.dumps(item, default=str))
#             else:
#                 parts.append(str(item))
#         return "\n".join(parts)

#     return str(content)


# def _try_parse_payload(text_value: str):
#     raw = (text_value or "").strip()
#     if not raw:
#         return None

#     try:
#         return json.loads(raw)
#     except Exception:
#         pass

#     try:
#         return ast.literal_eval(raw)
#     except Exception:
#         return None


# def _extract_html_from_payload(payload: Any) -> str:
#     if payload is None:
#         return ""

#     if isinstance(payload, dict):
#         html_value = payload.get("html")
#         if isinstance(html_value, str) and html_value.strip():
#             cleaned_html = html_value.strip()

#             # real HTML
#             if "<!doctype html>" in cleaned_html.lower() or "<html" in cleaned_html.lower():
#                 return cleaned_html

#             # nested JSON string like {"html":"<!DOCTYPE html>...</html>"}
#             parsed_inner = _try_parse_payload(cleaned_html)
#             if parsed_inner is not None:
#                 inner_html = _extract_html_from_payload(parsed_inner)
#                 if inner_html:
#                     return inner_html

#         # scan all dict values
#         for value in payload.values():
#             found = _extract_html_from_payload(value)
#             if found:
#                 return found
#         return ""

#     if isinstance(payload, list):
#         for item in payload:
#             found = _extract_html_from_payload(item)
#             if found:
#                 return found
#         return ""

#     if isinstance(payload, str):
#         text_value = payload.strip()
#         if not text_value:
#             return ""

#         # direct HTML string
#         if "<!doctype html>" in text_value.lower() or "<html" in text_value.lower():
#             return text_value

#         # fenced HTML
#         code_match = re.search(
#             r"```html\s*(.*?)```",
#             text_value,
#             flags=re.IGNORECASE | re.DOTALL,
#         )
#         if code_match:
#             candidate = code_match.group(1).strip()
#             if candidate:
#                 return candidate

#         # JSON string containing html
#         parsed = _try_parse_payload(text_value)
#         if parsed is not None:
#             return _extract_html_from_payload(parsed)

#     return ""


# def _collect_agent_trace(messages: list[Any]) -> list[dict]:
#     trace = []
#     for index, msg in enumerate(messages):
#         trace.append(
#             {
#                 "index": index,
#                 "type": type(msg).__name__,
#                 "name": getattr(msg, "name", None),
#                 "content_preview": _extract_text_from_content(
#                     getattr(msg, "content", "")
#                 )[:1500],
#             }
#         )
#     return trace


# def _apply_schema_overrides(user_query: str, schema_dict: dict) -> dict:
#     normalized_query = user_query.lower().strip()

#     table_keywords = [
#         "table",
#         "details",
#         "employee details",
#         "records",
#         "list",
#         "show employees",
#         "employee table",
#     ]
#     chart_keywords = [
#         "chart",
#         "graph",
#         "bar chart",
#         "line chart",
#         "pie chart",
#         "scatter",
#         "plot",
#         "visualize",
#     ]
#     metric_keywords = [
#         "kpi",
#         "metric",
#         "summary",
#         "count",
#         "average",
#         "avg",
#         "total",
#     ]
#     dashboard_keywords = [
#         "dashboard",
#         "overview",
#         "analysis",
#         "analytics",
#         "compare",
#         "comparison",
#     ]

#     wants_table = any(word in normalized_query for word in table_keywords)
#     wants_chart = any(word in normalized_query for word in chart_keywords)
#     wants_metric = any(word in normalized_query for word in metric_keywords)
#     wants_dashboard = any(word in normalized_query for word in dashboard_keywords)

#     if wants_table and not wants_dashboard and not wants_chart:
#         schema_dict["required_metrics"] = []
#         schema_dict["charts"] = []
#         if not schema_dict.get("tables"):
#             schema_dict["tables"] = ["Employee Details Table"]
#         schema_dict["title"] = schema_dict.get("title") or "Employee Details"

#     elif wants_chart and not wants_dashboard and not wants_table:
#         schema_dict["required_metrics"] = []
#         schema_dict["tables"] = []
#         if not schema_dict.get("charts"):
#             schema_dict["charts"] = ["Employee Chart"]

#     elif wants_metric and not wants_dashboard and not wants_table and not wants_chart:
#         schema_dict["charts"] = []
#         schema_dict["tables"] = []
#         if not schema_dict.get("required_metrics"):
#             schema_dict["required_metrics"] = ["Summary Metric"]

#     schema_dict.setdefault("title", "Employee Dashboard")
#     schema_dict.setdefault("required_metrics", [])
#     schema_dict.setdefault("charts", [])
#     schema_dict.setdefault("tables", [])
#     return schema_dict


# def execute_crm_pipeline(user_query: str) -> dict:
#     llm = get_llm()

#     planner_prompt = ChatPromptTemplate.from_messages(
#         [
#             (
#                 "system",
#                 "You are the Senior UI Planner for an employment analytics dashboard. "
#                 "The only available source table is 'employees' with EXACT columns: "
#                 f"{', '.join(ALLOWED_COLUMNS)}. "
#                 "Respect user intent tightly. "
#                 "If the user asks for only a table, return only tables. "
#                 "If the user asks for only a chart, return only charts. "
#                 "If the user asks for only a KPI or summary, return only required_metrics. "
#                 "Use multiple widget types only when the user clearly wants an overview, dashboard, analysis, or comparison.",
#             ),
#             (
#                 "human",
#                 "User Request: {query}\n"
#                 "Return a structured schema with title, required_metrics, charts, and tables.\n"
#                 "Do not add unnecessary widget types.",
#             ),
#         ]
#     )

#     planner_chain = planner_prompt | llm.with_structured_output(PlannerSchemaOut)
#     schema_plan = planner_chain.invoke({"query": user_query})
#     schema_dict = _apply_schema_overrides(user_query, _to_schema_dict(schema_plan))
#     schema_json = json.dumps(schema_dict, indent=2, default=str)

#     widget_count = (
#         len(schema_dict.get("required_metrics", []))
#         + len(schema_dict.get("charts", []))
#         + len(schema_dict.get("tables", []))
#     )

#     resolver_prompt = ChatPromptTemplate.from_messages(
#         [
#             (
#                 "system",
#                 "You are a PostgreSQL query planner. Generate ONE valid SQL query using ONLY the 'employees' table "
#                 f"and ONLY these columns: {', '.join(ALLOWED_COLUMNS)}. "
#                 "Do not invent columns. Do not use any other table. Prefer returning the lowest-grain practical rows "
#                 "needed so downstream reasoning can calculate totals, averages, comparisons, distributions, rankings, and tables from real data.",
#             ),
#             (
#                 "human",
#                 "UI Schema Requirement:\n{schema}\n\n"
#                 "Return the SQL required to fetch the data needed for this response.",
#             ),
#         ]
#     )

#     resolver_chain = resolver_prompt | llm.with_structured_output(DBResolverPlanOut)
#     db_plan = resolver_chain.invoke({"schema": schema_json})
#     real_data_json = _execute_sql(db_plan.sql_query)

#     ui_tools = [
#         generate_crm_metric,
#         generate_crm_table,
#         generate_crm_bar_chart,
#         generate_crm_line_chart,
#         generate_crm_pie_chart,
#         generate_crm_scatter_plot,
#         generate_dashboard_html,
#     ]

#     system_prompt = """
# You are an Autonomous Data-Driven UI Agent.

# Your job is to generate FINAL HTML yourself.
# The frontend will only render the final HTML returned by your tool output.

# You are not just a UI formatter.
# You are responsible for:
# - understanding the user intent,
# - inspecting the provided real data,
# - performing correct calculations when needed,
# - selecting the most truthful and useful widget type from the provided schema,
# - and generating a polished final HTML UI.

# ==================================================
# CORE RESPONSIBILITY
# ==================================================

# You will receive:
# 1. User Intent
# 2. Planned UI Schema
# 3. Planned Widget Count
# 4. Real Data

# You MUST use the provided Real Data as the primary source of truth.

# You MUST inspect that real data before creating any widget.

# You MUST ensure that:
# - displayed values come from real data,
# - calculated values are computed correctly,
# - charts are grounded in real columns and real values,
# - titles match what is actually shown.

# ==================================================
# STRICT RULES
# ==================================================

# 1. Do not return raw JSON.
# 2. Do not return markdown code fences unless they are intentionally inside HTML content.
# 3. Do not ask the frontend to assemble anything.
# 4. Use inline CSS only.
# 5. Prefer pure HTML/CSS and inline SVG for charts.
# 6. You must always end with a tool output that already contains a complete HTML document.
# 7. Build the UI strictly from the provided schema.
# 8. If schema.tables is non-empty and schema.charts and schema.required_metrics are empty, return only a table UI.
# 9. If schema.charts is non-empty and schema.tables and schema.required_metrics are empty, return only a chart UI.
# 10. If schema.required_metrics is non-empty and schema.tables and schema.charts are empty, return only a KPI/metric UI.
# 11. Do not invent extra widgets beyond the schema.
# 12. For single-widget requests, call exactly one widget tool with standalone=True and stop there.
# 13. For multi-widget requests, call widget tools with standalone=False, combine their widget HTML, and then call generate_dashboard_html.
# 14. Do not call generate_dashboard_html for single-widget requests.
# 15. The final valid answer is only the tool payload containing an html field.
# 16. Never invent fake values, fake columns, fake categories, fake sequences, or fake trends.
# 17. Never label something as time trend, monthly trend, yearly trend, or growth trend unless the real data actually supports that.
# 18. If the schema asks for a chart type that is not honestly supported by the real data, you must still satisfy the schema as closely as possible but keep the chart title and subtitle truthful to the actual data being shown.
# 19. Do not output explanations outside the final tool chain.
# 20. Do not ignore the real data even if the schema is vague.
# 21. The tools do not provide styling or theme logic. You must decide the full visual design yourself inside the HTML you pass to the tools.

# ==================================================
# DATA INTERPRETATION RULES
# ==================================================

# Before building UI, you must inspect the provided Real Data and determine:
# - what fields are present,
# - which fields are numeric,
# - which fields are categorical,
# - whether any field is time-like or ordered,
# - whether grouping or aggregation is needed,
# - whether calculations are needed to satisfy the user intent and schema.

# You must treat Real Data as the truth source.

# If a KPI is requested:
# - derive it from real data,
# - calculate correctly,
# - format clearly.

# If a chart is requested:
# - use only real columns and real values,
# - group and aggregate correctly where needed,
# - choose axes that honestly represent the data.

# If a table is requested:
# - show clean structured records from the real data,
# - make it readable,
# - preserve meaning.

# ==================================================
# CALCULATION RULES
# ==================================================

# When the user intent or schema implies calculation, you must compute properly from the provided real data.

# Examples of valid calculations:
# - count
# - sum
# - average
# - min
# - max
# - percentage
# - ratio
# - ranking
# - grouped comparison
# - distribution
# - share of total

# Rules:
# - always compute using real values,
# - never guess missing values,
# - round numbers sensibly,
# - preserve business readability,
# - prefer meaningful summary over noisy raw output when appropriate.

# If the user asks for comparison:
# - compare on real grouped values.

# If the user asks for distribution:
# - use grouped counts or grouped sums as appropriate.

# If the user asks for trend:
# - only show a trend-like chart when the real data supports a sequential or ordered interpretation.

# ==================================================
# CHART HONESTY RULES
# ==================================================

# Use the chart type requested by schema whenever it is honestly possible from the data.

# But before rendering a chart, verify whether the data supports it properly.

# LINE CHART:
# - Use only when there is a real sequence, such as:
#   - date
#   - month
#   - year
#   - week
#   - time
#   - ordered bucket
#   - ranking
#   - progression-like ordered dimension

# - If no real sequential or ordered dimension exists, do not fake a trend.
# - If schema still requires a line chart, use the most honest ordered representation you can derive from the real data and title it truthfully.

# BAR CHART:
# - Use for category comparisons.
# - This is the safest comparison chart.

# PIE CHART:
# - Use only for share/distribution across a small number of categories.

# SCATTER PLOT:
# - Use only for numeric vs numeric relationships.

# TABLE:
# - Use for records, breakdowns, rankings, and structured detail.

# KPI/METRIC:
# - Use for headline numbers and summaries.

# ==================================================
# SCHEMA DISCIPLINE RULES
# ==================================================

# You must obey the schema structure.

# Do not invent extra widgets beyond the schema.

# Within each requested widget, you must still:
# - choose the best truthful representation,
# - perform proper calculations,
# - format values correctly,
# - and create a premium UI.

# ==================================================
# TOOL RULES
# ==================================================

# - Use generate_crm_metric for KPI cards and summary numbers.
# - Use generate_crm_table for detailed records and structured tables.
# - Use generate_crm_bar_chart for category comparisons.
# - Use generate_crm_line_chart for trend-like or ordered views.
# - Use generate_crm_pie_chart for distribution views.
# - Use generate_crm_scatter_plot for relationship views.
# - Use generate_dashboard_html only when there are multiple widgets in the final response.

# For single-widget requests:
# - call exactly one widget tool,
# - pass standalone=True,
# - stop there.

# For multi-widget requests:
# - call widget tools with standalone=False,
# - combine their widget HTML,
# - then call generate_dashboard_html.

# ==================================================
# HTML QUALITY RULES
# ==================================================

# - Build a professional business-friendly UI.
# - Make it visually premium, not plain.
# - Use polished spacing and alignment.
# - Use balanced colors.
# - Use elegant contrast.
# - Add hover effects where possible.
# - Keep typography clean and readable.
# - Make cards attractive and modern.
# - Make tables scroll safely on smaller screens.
# - Make charts readable and well-labeled.
# - Do not output placeholders.
# - Do not output incomplete widgets.
# - Do not produce childish or flashy UI.
# - Prefer refined enterprise-style dashboard aesthetics.

# ==================================================
# SELF-CHECK BEFORE FINAL TOOL OUTPUT
# ==================================================

# Before producing the final tool output, internally verify:
# 1. Did I use the provided real data?
# 2. Are the displayed values grounded in that real data?
# 3. Did I calculate correctly where needed?
# 4. Does the chart honestly match the available data?
# 5. Are the title and subtitle truthful?
# 6. Did I obey the schema exactly?
# 7. Did I avoid extra widgets?
# 8. Is the final UI polished enough to feel production-like?

# ==================================================
# FINAL OUTPUT RULE
# ==================================================

# The final valid answer is ONLY the final tool output payload containing the html field.
# Do not include commentary before or after it.
# """

#     react_agent = create_react_agent(
#         llm,
#         tools=ui_tools,
#         prompt=system_prompt,
#     )

#     user_message = f"""
# User Intent:
# {user_query}

# Planned UI Schema:
# {schema_json}

# Planned Widget Count:
# {widget_count}

# Decision Rule:
# - If widget count is 1 or less, return exactly one standalone widget page.
# - If widget count is more than 1, return one combined dashboard page.

# Real Data Source of Truth:
# The following JSON contains the actual data returned from the database.
# You must inspect it before creating any widget.
# You must use this data for all calculations, summaries, chart values, labels, and tables.
# You must not invent any values beyond what can be derived from this data.

# Real Data JSON:
# {real_data_json}

# Build the final UI now using your tools.
# """

#     result = react_agent.invoke({"messages": [("user", user_message)]})

#     html_output = ""
#     for msg in reversed(result["messages"]):
#         payload_text = _extract_text_from_content(getattr(msg, "content", ""))
#         html_candidate = _extract_html_from_payload(payload_text)
#         if html_candidate:
#             html_output = html_candidate.strip()
#             break

#     if not html_output:
#         for msg in reversed(result["messages"]):
#             if getattr(msg, "name", "") in {
#                 "generate_dashboard_html",
#                 "generate_crm_metric",
#                 "generate_crm_table",
#                 "generate_crm_bar_chart",
#                 "generate_crm_line_chart",
#                 "generate_crm_pie_chart",
#                 "generate_crm_scatter_plot",
#             }:
#                 payload_text = _extract_text_from_content(getattr(msg, "content", ""))
#                 html_candidate = _extract_html_from_payload(payload_text)
#                 if html_candidate:
#                     html_output = html_candidate.strip()
#                     break

#     if not html_output:
#         agent_trace = _collect_agent_trace(result["messages"])
#         raise RuntimeError(
#             "UI agent failed to generate final HTML. "
#             f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
#         )

#     # Final safety unwrap in case html_output is still a JSON string
#     parsed_final = _try_parse_payload(html_output)
#     if parsed_final is not None:
#         unwrapped_html = _extract_html_from_payload(parsed_final)
#         if unwrapped_html:
#             html_output = unwrapped_html.strip()

#     if "<html" not in html_output.lower():
#         agent_trace = _collect_agent_trace(result["messages"])
#         raise RuntimeError(
#             "UI agent returned content, but it is not a full HTML document. "
#             f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
#         )

#     return {
#         "html": html_output,
#         "schema_plan": schema_dict,
#         "insights": [
#             {
#                 "title": "Premium Agentic UI",
#                 "description": "Agent 3 now reasons more strongly about real data, calculations, and truthful visualization before composing final HTML.",
#                 "type": "success",
#             }
#         ],
#         "data_summary": (
#             real_data_json[:1200] + "... (truncated)"
#             if len(real_data_json) > 1200
#             else real_data_json
#         ),
#     }
import ast
import json
import re
from typing import Any, Generator

from sqlalchemy import text
from langgraph.prebuilt import create_react_agent
from langchain_core.prompts import ChatPromptTemplate

from app.core.llm import get_llm
from app.schemas.crm import PlannerSchemaOut, DBResolverPlanOut
from app.db.database import SessionLocal
from app.agents.tools import (
    generate_crm_metric,
    generate_crm_table,
    generate_crm_bar_chart,
    generate_crm_line_chart,
    generate_crm_pie_chart,
    generate_crm_scatter_plot,
    generate_dashboard_html,
)


ALLOWED_COLUMNS = [
    "id",
    "name",
    "department",
    "performance_score",
    "attendance_pct",
    "attrition_score",
    "salary",
]


def _make_event(event_type: str, node: str, message: str, data: dict | None = None) -> dict:
    return {
        "event_type": event_type,
        "node": node,
        "message": message,
        "data": data or {},
    }


def _to_schema_dict(schema_obj: Any) -> dict:
    if hasattr(schema_obj, "model_dump"):
        return schema_obj.model_dump()
    if hasattr(schema_obj, "dict"):
        return schema_obj.dict()
    return dict(schema_obj)


def _execute_sql(sql_query: str) -> str:
    """Legacy single-query execution (kept for fallback)."""
    db = SessionLocal()
    try:
        result = db.execute(text(sql_query))
        rows = result.mappings().all()
        data = [dict(row) for row in rows]
        return json.dumps(data, indent=2, default=str)
    finally:
        db.close()


def _execute_sql_queries(sql_queries: list[str]) -> dict:
    """Execute multiple SQL queries and return a dict keyed by dataset index."""
    db = SessionLocal()
    results = {}
    try:
        for idx, query in enumerate(sql_queries):
            result = db.execute(text(query))
            rows = result.mappings().all()
            data = [dict(row) for row in rows]
            results[f"dataset_{idx}"] = data
        return results
    finally:
        db.close()


def _build_data_profile(real_data_json: str) -> dict:
    """
    Handles both:
    - single dataset (JSON array)
    - multi-dataset (JSON object with keys like dataset_0, dataset_1)
    """
    data = _try_parse_payload(real_data_json)

    # Multi-dataset object -> use first dataset for profile (or combine)
    if isinstance(data, dict) and all(isinstance(v, list) for v in data.values()):
        # Use the first dataset to build the profile
        first_key = next(iter(data))
        rows = data[first_key]
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


def _extract_text_from_content(content: Any) -> str:
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, dict):
        return json.dumps(content, default=str)

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif "content" in item:
                    parts.append(str(item["content"]))
                else:
                    parts.append(json.dumps(item, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)

    return str(content)


def _try_parse_payload(text_value: str):
    raw = (text_value or "").strip()
    if not raw:
        return None

    try:
        return json.loads(raw)
    except Exception:
        pass

    try:
        return ast.literal_eval(raw)
    except Exception:
        return None


def _extract_html_from_payload(payload: Any) -> str:
    if payload is None:
        return ""

    if isinstance(payload, dict):
        html_value = payload.get("html")
        if isinstance(html_value, str) and html_value.strip():
            cleaned_html = html_value.strip()

            if "<!doctype html>" in cleaned_html.lower() or "<html" in cleaned_html.lower():
                return cleaned_html

            parsed_inner = _try_parse_payload(cleaned_html)
            if parsed_inner is not None:
                inner_html = _extract_html_from_payload(parsed_inner)
                if inner_html:
                    return inner_html

        for value in payload.values():
            found = _extract_html_from_payload(value)
            if found:
                return found
        return ""

    if isinstance(payload, list):
        for item in payload:
            found = _extract_html_from_payload(item)
            if found:
                return found
        return ""

    if isinstance(payload, str):
        text_value = payload.strip()
        if not text_value:
            return ""

        if "<!doctype html>" in text_value.lower() or "<html" in text_value.lower():
            return text_value

        code_match = re.search(
            r"```html\s*(.*?)```",
            text_value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if code_match:
            candidate = code_match.group(1).strip()
            if candidate:
                return candidate

        parsed = _try_parse_payload(text_value)
        if parsed is not None:
            return _extract_html_from_payload(parsed)

    return ""


def _validate_generated_html(html_output: str) -> tuple[bool, list[str]]:
    issues = []
    html_lower = html_output.lower()

    if "<html" not in html_lower:
        issues.append("Missing <html> tag")

    if "<body" not in html_lower:
        issues.append("Missing <body> tag")

    if "<svg" in html_lower and "viewbox" not in html_lower:
        issues.append("SVG exists without viewBox")

    if "<table" in html_lower and "<thead" not in html_lower:
        issues.append("Table exists without thead")

    if "```" in html_output:
        issues.append("Markdown fence leaked into HTML")

    return len(issues) == 0, issues


def _collect_agent_trace(messages: list[Any]) -> list[dict]:
    trace = []
    for index, msg in enumerate(messages):
        trace.append(
            {
                "index": index,
                "type": type(msg).__name__,
                "name": getattr(msg, "name", None),
                "content_preview": _extract_text_from_content(
                    getattr(msg, "content", "")
                )[:1500],
            }
        )
    return trace


def _ensure_component_defaults(component: dict) -> dict:
    comp = dict(component or {})

    comp.setdefault("type", "table")
    comp.setdefault("title", "Untitled Component")
    comp.setdefault("data_source", "employees")
    comp.setdefault("intent", "detailed_records")
    comp.setdefault("columns", [])
    comp.setdefault("filters", {})
    comp.setdefault("group_by", [])
    comp.setdefault("aggregation", None)
    comp.setdefault("sort_by", [])
    comp.setdefault("limit", None)
    comp.setdefault("pagination", False)
    comp.setdefault("sortable", False)
    comp.setdefault("category_field", None)
    comp.setdefault("value_field", None)

    cleaned_columns = [str(c).strip() for c in comp["columns"] if str(c).strip()]
    if not cleaned_columns:
        if comp["type"] == "table":
            cleaned_columns = ["name", "department", "salary", "performance_score"]
        elif comp["type"] in {"bar_chart", "line_chart", "pie_chart"}:
            cleaned_columns = ["department", "salary"]
        elif comp["type"] == "scatter_plot":
            cleaned_columns = ["salary", "performance_score"]
        elif comp["type"] == "metric":
            cleaned_columns = ["salary"]
        else:
            cleaned_columns = ["name", "department"]

    comp["columns"] = cleaned_columns

    if comp["type"] in {"pie_chart", "bar_chart", "line_chart"} and not comp.get("category_field"):
        comp["category_field"] = comp["columns"][0] if comp["columns"] else None

    if comp["type"] in {"pie_chart", "bar_chart", "line_chart", "scatter_plot"} and not comp.get("value_field"):
        if len(comp["columns"]) > 1:
            comp["value_field"] = comp["columns"][1]
        elif comp["type"] != "pie_chart":
            comp["value_field"] = comp["columns"][0]

    return comp


def _apply_schema_overrides(user_query: str, schema_dict: dict) -> dict:
    normalized_query = user_query.lower().strip()

    schema_dict.setdefault("type", "dashboard")
    schema_dict.setdefault("title", "Employee Dashboard")
    schema_dict.setdefault("layout", "grid")
    schema_dict.setdefault("primary_data_source", "employees")
    schema_dict.setdefault("user_intent", user_query)
    schema_dict.setdefault("components", [])

    schema_dict.setdefault("required_metrics", [])
    schema_dict.setdefault("charts", [])
    schema_dict.setdefault("tables", [])

    wants_table = any(
        word in normalized_query
        for word in ["table", "details", "employee details", "records", "list", "show employees", "employee table"]
    )
    wants_chart = any(
        word in normalized_query
        for word in ["chart", "graph", "bar chart", "line chart", "pie chart", "scatter", "plot", "visualize"]
    )
    wants_metric = any(
        word in normalized_query
        for word in ["kpi", "metric", "summary", "count", "average", "avg", "total"]
    )
    wants_dashboard = any(
        word in normalized_query
        for word in ["dashboard", "overview", "analysis", "analytics", "compare", "comparison"]
    )

    components = [_ensure_component_defaults(c) for c in schema_dict.get("components", [])]

    if not components:
        if wants_table and not wants_chart and not wants_metric and not wants_dashboard:
            components = [
                {
                    "type": "table",
                    "title": "Employee Details",
                    "data_source": "employees",
                    "intent": "detailed_records",
                    "columns": ["name", "department", "salary", "performance_score"],
                    "filters": {},
                    "group_by": [],
                    "aggregation": None,
                    "sort_by": [],
                    "limit": 20,
                    "pagination": True,
                    "sortable": True,
                    "category_field": None,
                    "value_field": None,
                }
            ]
            schema_dict["type"] = "table"
            schema_dict["title"] = "Employee Details"

        elif wants_chart and not wants_table and not wants_metric and not wants_dashboard:
            components = [
                {
                    "type": "bar_chart",
                    "title": "Department Comparison",
                    "data_source": "employees",
                    "intent": "comparison",
                    "columns": ["department", "salary"],
                    "filters": {},
                    "group_by": ["department"],
                    "aggregation": {"metric": "avg", "field": "salary"},
                    "sort_by": [],
                    "limit": None,
                    "pagination": False,
                    "sortable": False,
                    "category_field": "department",
                    "value_field": "salary",
                }
            ]
            schema_dict["type"] = "bar_chart"

        elif wants_metric and not wants_table and not wants_chart and not wants_dashboard:
            components = [
                {
                    "type": "metric",
                    "title": "Average Salary",
                    "data_source": "employees",
                    "intent": "summary_metric",
                    "columns": ["salary"],
                    "filters": {},
                    "group_by": [],
                    "aggregation": {"metric": "avg", "field": "salary"},
                    "sort_by": [],
                    "limit": None,
                    "pagination": False,
                    "sortable": False,
                    "category_field": None,
                    "value_field": "salary",
                }
            ]
            schema_dict["type"] = "metric"

    components = [_ensure_component_defaults(c) for c in components]
    schema_dict["components"] = components

    if len(components) == 1:
        single = components[0]
        schema_dict["type"] = single["type"]
        schema_dict["data_source"] = single["data_source"]
        schema_dict["columns"] = single["columns"]
        schema_dict["filters"] = single["filters"]
        schema_dict["pagination"] = single["pagination"]
        schema_dict["sortable"] = single["sortable"]
        schema_dict["group_by"] = single["group_by"]
        schema_dict["aggregation"] = single["aggregation"]
        schema_dict["sort_by"] = single["sort_by"]
        schema_dict["category_field"] = single.get("category_field")
        schema_dict["value_field"] = single.get("value_field")
    else:
        schema_dict["type"] = "dashboard"

    return schema_dict


def _get_ui_tools():
    return [
        generate_crm_metric,
        generate_crm_table,
        generate_crm_bar_chart,
        generate_crm_line_chart,
        generate_crm_pie_chart,
        generate_crm_scatter_plot,
        generate_dashboard_html,
    ]


def _get_ui_system_prompt() -> str:
    return """
You are an Autonomous Data-Driven UI Agent. Generate FINAL HTML only.

Inputs:
- User Intent
- Planned UI Schema (single-widget or dashboard with components array)
- Component Count
- Real Data (JSON object with keys like "dataset_0", "dataset_1", … or a single array)

==================================================
ABSOLUTE RULES
==================================================
1. Use ONLY the provided schema structure. No extra widgets.
2. Use real data exclusively. Never invent values, columns, categories, or placeholders.
3. Tables: iterate every row; render columns in exact schema order; show empty string only if value is truly null/undefined. Never use "-" as fake placeholder.
4. Inline CSS only. No <style> tags.
5. Return ONLY tool payload containing full HTML field. No markdown fences, no commentary.

==================================================
MULTI-DATASET HANDLING
==================================================
Real Data may be an object like `{"dataset_0": [...], "dataset_1": [...]}`.
- One component → use `dataset_0`.
- Multiple components → map `dataset_0` to first component, `dataset_1` to second, etc.
- If a component expects aggregated data but receives raw rows, perform the aggregation yourself using schema's `group_by`/`aggregation` hints.

==================================================
COMPONENT MAPPING
==================================================
metric       → generate_crm_metric
table        → generate_crm_table
bar_chart    → generate_crm_bar_chart
line_chart   → generate_crm_line_chart
pie_chart    → generate_crm_pie_chart
scatter_plot → generate_crm_scatter_plot

Component count == 1 → call widget tool with standalone=True, then stop.
Component count  > 1 → call tools with standalone=False, combine widget HTML, then call generate_dashboard_html once.

==================================================
MANDATORY DATA INSPECTION (DO THIS FIRST)
==================================================
- Parse Real Data JSON completely.
- Identify all columns.
- For grouped charts: aggregate values per unique category. Use a dictionary/map to ensure each category appears exactly once. Sort categories logically (alphabetically or by value).
- For tables: use raw rows directly.

==================================================
SCALING & AXIS RULES (CRITICAL – PREVENTS MISLEADING BARS)
==================================================
Bar Chart Y‑Axis Domain (choose based on value field):
- **attendance_pct** → fixed domain 0 to 100
- **attrition_score** → fixed domain 0 to 1
- For all other numeric fields → domain 0 to (max_value * 1.1) (add 10% headroom)

Bar Height Calculation (chart_bottom=200, chart_top=40, chart_height=160):
  normalized = value / domain_max   (clamp to 1.0 if value > domain_max)
  bar_height = normalized * chart_height
  bar_y = chart_bottom - bar_height
  <rect x="..." y="bar_y" width="40" height="bar_height" rx="4" />

Always draw y‑axis ticks and labels:
- For percentage fields: label 0%, 25%, 50%, 75%, 100%
- For attrition: label 0.0, 0.25, 0.5, 0.75, 1.0
- For other fields: label 0, 25% of max, 50% of max, 75% of max, max (rounded)

==================================================
CHART HONESTY & FALLBACK
==================================================
- Line chart: only when real sequential/ordered dimension exists.
- Bar chart: for grouped comparison.
- Pie chart: only for meaningful share/distribution.
- Scatter plot: only numeric vs numeric.

If a chart cannot be truthfully rendered → fallback to a clean table or metric card with a brief note.

==================================================
LABEL ANTI‑COLLISION
==================================================
- Start with label_y = bar_y - 8.
- If the previous label's y is within 20px of this label_y, shift this label up by 15px (repeat if needed).
- Ensure no two labels overlap.

==================================================
VISUAL DESIGN (PRODUCTION‑GRADE, AESTHETIC‑RICH)
==================================================
You have creative freedom to design a beautiful, modern dashboard. Follow these guidelines:

- **Color Palette**: Use a cohesive, multi‑hue palette (e.g., blues, emeralds, ambers, violets). Do not use a single color for all bars. Gradients are welcome.
- **Cards**: White background, generous border radius (16–20px), soft shadows, subtle border.
- **Typography**: System sans‑serif fonts, clean hierarchy.
- **Spacing**: Ample padding, balanced whitespace.
- **Tables**: Clean, striped rows optional, hover effect optional, sticky header.
- **Metrics**: Large, bold numbers with subtle labels.
- **Charts**: Include grid lines or axis ticks for readability. Axes should be visible but not overpowering.

The goal is a premium, dashboard‑style UI that looks like it belongs in a modern business intelligence tool.

==================================================
FINAL OUTPUT
==================================================
Return ONLY the tool payload containing the full HTML. No extra text.
"""


def _run_planner_stage(llm, user_query: str) -> tuple[dict, str, int]:
    planner_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a Retrieval-Aware UI Planner for an employment analytics system. "
                "The currently available data source is 'employees'. "
                f"The currently known candidate columns are: {', '.join(ALLOWED_COLUMNS)}. "
                "Your job is to produce a richer UI schema that helps the SQL agent find relevant data. "
                "Keep common existing fields like title, layout, primary_data_source, user_intent, and components. "
                "Also add richer fields such as type, data_source, columns, filters, group_by, aggregation, sort_by, pagination, sortable, category_field, value_field. "
                "Columns is mandatory for every component and must never be empty. "
                "For single-widget responses, the root schema may also directly expose fields like type, data_source, columns, filters, pagination, sortable. "
                "For dashboards, use components. "
                "Return a structured schema only.",
            ),
            (
                "human",
                "User Request: {query}\n\n"
                "Create the best retrieval-aware UI schema for this request.\n"
                "Mandatory rule: every component must contain a non-empty columns list.\n"
                "Use employees as data_source unless the request clearly implies another source.\n"
                "Keep old/common fields, but enrich schema with new fields.",
            ),
        ]
    )

    planner_chain = planner_prompt | llm.with_structured_output(
        PlannerSchemaOut,
        method="function_calling"
    )
    schema_plan = planner_chain.invoke({"query": user_query})
    schema_dict = _apply_schema_overrides(user_query, _to_schema_dict(schema_plan))
    schema_json = json.dumps(schema_dict, indent=2, default=str)
    component_count = len(schema_dict.get("components", []))
    return schema_dict, schema_json, component_count


def _run_sql_resolver_stage(llm, schema_json: str):
    resolver_prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are a PostgreSQL query planner. "
            f"Use ONLY the 'employees' table and columns: {', '.join(ALLOWED_COLUMNS)}. "
            "The UI schema may contain multiple components. "
            "Return a list of SQL queries (`sql_queries`). Each query should produce ONE clean, self-contained result set for a specific component. "
            "Do NOT use UNION to combine different schemas (e.g., raw rows with aggregates). "
            "Each query must be valid PostgreSQL and have a consistent column set. "
            "Example: ['SELECT * FROM employees LIMIT 20;', 'SELECT department, AVG(salary) AS avg_salary FROM employees GROUP BY department;']"
        ),
        (
            "human",
            "UI Schema Requirement:\n{schema}\n\nReturn a list of SQL queries (`sql_queries`)."
        ),
    ])

    resolver_chain = resolver_prompt | llm.with_structured_output(
        DBResolverPlanOut,
        method="function_calling"
    )
    return resolver_chain.invoke({"schema": schema_json})


def _run_ui_agent_stage(
    llm,
    user_query: str,
    schema_json: str,
    component_count: int,
    real_data_json: str,
) -> str:
    react_agent = create_react_agent(
        llm,
        tools=_get_ui_tools(),
        prompt=_get_ui_system_prompt(),
    )

    data_profile = _build_data_profile(real_data_json)

    user_message = f"""
User Intent:
{user_query}

Planned Retrieval-Aware UI Schema:
{schema_json}

Planned Component Count:
{component_count}

Decision Rule:
- If component count is 1, return exactly one standalone widget page.
- If component count is more than 1, return one combined dashboard page.

Real Data Source of Truth:
The following JSON contains the actual data returned from the database.
You must inspect it before creating any widget.
You must use this data for all calculations, summaries, chart values, labels, and tables.
You must not invent any values beyond what can be derived from this data.

Data Profile:
{json.dumps(data_profile, indent=2)}

Real Data JSON:
{real_data_json}

Build the final UI now using your tools.
"""

    result = react_agent.invoke({"messages": [("user", user_message)]})

    html_output = ""
    for msg in reversed(result["messages"]):
        payload_text = _extract_text_from_content(getattr(msg, "content", ""))
        html_candidate = _extract_html_from_payload(payload_text)
        if html_candidate:
            html_output = html_candidate.strip()
            break

    if not html_output:
        for msg in reversed(result["messages"]):
            if getattr(msg, "name", "") in {
                "generate_dashboard_html",
                "generate_crm_metric",
                "generate_crm_table",
                "generate_crm_bar_chart",
                "generate_crm_line_chart",
                "generate_crm_pie_chart",
                "generate_crm_scatter_plot",
            }:
                payload_text = _extract_text_from_content(getattr(msg, "content", ""))
                html_candidate = _extract_html_from_payload(payload_text)
                if html_candidate:
                    html_output = html_candidate.strip()
                    break

    if not html_output:
        agent_trace = _collect_agent_trace(result["messages"])
        raise RuntimeError(
            "UI agent failed to generate final HTML. "
            f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
        )

    parsed_final = _try_parse_payload(html_output)
    if parsed_final is not None:
        unwrapped_html = _extract_html_from_payload(parsed_final)
        if unwrapped_html:
            html_output = unwrapped_html.strip()

    if "<html" not in html_output.lower():
        agent_trace = _collect_agent_trace(result["messages"])
        raise RuntimeError(
            "UI agent returned content, but it is not a full HTML document. "
            f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
        )

    is_valid_html, html_issues = _validate_generated_html(html_output)
    if not is_valid_html:
        agent_trace = _collect_agent_trace(result["messages"])
        raise RuntimeError(
            "UI agent returned invalid HTML structure. "
            f"Issues: {html_issues}. "
            f"Agent trace: {json.dumps(agent_trace, indent=2, default=str)}"
        )

    return html_output


def _build_final_result(html_output: str, schema_dict: dict, real_data_json: str) -> dict:
    return {
        "html": html_output,
        "schema_plan": schema_dict,
        "insights": [
            {
                "title": "Retrieval-Aware Agentic UI",
                "description": "Agent 1 now produces a richer UI schema with common existing fields plus new retrieval-oriented fields like columns, filters, grouping, and chart field hints.",
                "type": "success",
            }
        ],
        "data_summary": (
            real_data_json[:1200] + "... (truncated)"
            if len(real_data_json) > 1200
            else real_data_json
        ),
    }


def execute_crm_pipeline(user_query: str) -> dict:
    llm = get_llm()

    schema_dict, schema_json, component_count = _run_planner_stage(llm, user_query)
    db_plan = _run_sql_resolver_stage(llm, schema_json)

    # Prefer multiple queries if present
    if hasattr(db_plan, "sql_queries") and db_plan.sql_queries:
        real_data_dict = _execute_sql_queries(db_plan.sql_queries)
        real_data_json = json.dumps(real_data_dict, indent=2, default=str)
    elif hasattr(db_plan, "sql_query") and db_plan.sql_query:
        real_data_json = _execute_sql(db_plan.sql_query)
    else:
        real_data_json = "[]"

    html_output = _run_ui_agent_stage(
        llm=llm,
        user_query=user_query,
        schema_json=schema_json,
        component_count=component_count,
        real_data_json=real_data_json,
    )
    return _build_final_result(html_output, schema_dict, real_data_json)


def execute_crm_pipeline_stream(user_query: str) -> Generator[dict, None, None]:
    llm = get_llm()

    try:
        yield _make_event("stage_start", "query", "Query received")
        yield _make_event("stage_complete", "query", "Query accepted")

        # Intent Agent (formerly Agent 1)
        yield _make_event("edge_start", "query_to_intent_agent", "Connecting to Intent Agent")
        yield _make_event("stage_start", "intent_agent", "Intent Agent is planning the UI schema")

        schema_dict, schema_json, component_count = _run_planner_stage(llm, user_query)

        yield _make_event(
            "stage_complete",
            "intent_agent",
            "Intent Agent completed",
            {
                "title": schema_dict.get("title"),
                "component_count": component_count,
                "schema_plan": schema_dict,
            },
        )
        yield _make_event("edge_complete", "query_to_intent_agent", "Intent Agent connection finished")

        # Planning Agent (formerly Agent 2)
        yield _make_event("edge_start", "intent_to_planning_agent", "Connecting to Planning Agent")
        yield _make_event("stage_start", "planning_agent", "Planning Agent is generating SQL")

        db_plan = _run_sql_resolver_stage(llm, schema_json)

        yield _make_event(
            "stage_complete",
            "planning_agent",
            "Planning Agent completed",
            {
                "sql_queries": db_plan.sql_queries if hasattr(db_plan, "sql_queries") else [],
                "sql_query": db_plan.sql_query if hasattr(db_plan, "sql_query") else None,
            },
        )
        yield _make_event("edge_complete", "intent_to_planning_agent", "Planning Agent connection finished")

        yield _make_event("edge_start", "planning_to_db", "Connecting to database")
        yield _make_event("stage_start", "database", "Fetching real data from database")

        # Use multiple queries if available
        if hasattr(db_plan, "sql_queries") and db_plan.sql_queries:
            real_data_dict = _execute_sql_queries(db_plan.sql_queries)
            real_data_json = json.dumps(real_data_dict, indent=2, default=str)
            row_count = len(real_data_dict.get("dataset_0", []))
        elif hasattr(db_plan, "sql_query") and db_plan.sql_query:
            real_data_json = _execute_sql(db_plan.sql_query)
            real_data_preview = _try_parse_payload(real_data_json)
            row_count = len(real_data_preview) if isinstance(real_data_preview, list) else None
        else:
            real_data_json = "[]"
            row_count = 0

        yield _make_event(
            "stage_complete",
            "database",
            "Database fetch completed",
            {
                "row_count": row_count,
            },
        )
        yield _make_event("edge_complete", "planning_to_db", "Database connection finished")

        # UI Agent (formerly Agent 3)
        yield _make_event("edge_start", "db_to_ui_agent", "Passing real data to UI Agent")
        yield _make_event("stage_start", "ui_agent", "UI Agent is composing final UI")

        html_output = _run_ui_agent_stage(
            llm=llm,
            user_query=user_query,
            schema_json=schema_json,
            component_count=component_count,
            real_data_json=real_data_json,
        )

        yield _make_event("stage_complete", "ui_agent", "UI Agent completed")
        yield _make_event("edge_complete", "db_to_ui_agent", "UI Agent connection finished")

        yield _make_event("edge_start", "ui_agent_to_output", "Preparing final output")
        yield _make_event("stage_start", "output", "Finalizing HTML response")

        final_result = _build_final_result(html_output, schema_dict, real_data_json)

        yield _make_event("stage_complete", "output", "Final output ready")
        yield _make_event("edge_complete", "ui_agent_to_output", "Output connection finished")

        yield _make_event(
            "final_result",
            "output",
            "Response ready",
            final_result,
        )

    except Exception as e:
        yield _make_event(
            "error",
            "system",
            f"Pipeline failed: {str(e)}",
            {},
        )
