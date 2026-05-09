# import json
# import uuid
# from langchain_core.tools import tool

# @tool
# def create_table_tool(title: str, headers: list[str], rows: list[list[str]]) -> str:
#     """Generates an HTML table block. Provide a title, string headers list, and rows list representing row values."""
#     headers_html = "".join([f"<th style='padding: 12px; text-align: left; border-bottom: 2px solid #e5e7eb; color: #4b5563;'>{h}</th>" for h in headers])
#     rows_html = ""
#     for row in rows:
#         row_cells = "".join([f"<td style='padding: 12px; border-bottom: 1px solid #e5e7eb; color: #111827;'>{cell}</td>" for cell in row])
#         rows_html += f"<tr>{row_cells}</tr>"
        
#     return f"""
#     <div style="background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #eee; overflow-x: auto;">
#         <h3 style="margin-top: 0; color: #111827; font-size: 1.1rem; margin-bottom: 16px;">{title}</h3>
#         <table style="width: 100%; border-collapse: collapse; font-size: 0.9rem;">
#             <thead><tr>{headers_html}</tr></thead>
#             <tbody>{rows_html}</tbody>
#         </table>
#     </div>
#     """

# @tool
# def create_metric_widget_tool(title: str, value: str, subtext: str) -> str:
#     """Generates a beautiful HTML metric card widget. Useful for single stats like Total Employees or Average Salary."""
#     return f"""
#     <div style="background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #eee; display: flex; flex-direction: column;">
#         <span style="color: #6b7280; font-size: 0.875rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;">{title}</span>
#         <span style="color: #111827; font-size: 2rem; font-weight: 700; margin-top: 8px;">{value}</span>
#         <span style="color: #10b981; font-size: 0.875rem; margin-top: 4px; font-weight: 500;">{subtext}</span>
#     </div>
#     """

# @tool
# def create_bar_chart_tool(title: str, labels: list[str], values: list[float]) -> str:
#     """Generates a Bar Chart HTML/JS snippet. Pass in a title, a list of string labels (e.g. departments), and a list of numeric values."""
#     chart_id = "chart_" + str(uuid.uuid4().hex)[:8]
#     return f"""
#     <div style="background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #eee;">
#         <h3 style="margin-top: 0; color: #111827; font-size: 1.1rem;">{title}</h3>
#         <canvas id="{chart_id}"></canvas>
#         <script>
#             setTimeout(() => {{
#                 new Chart(document.getElementById('{chart_id}'), {{
#                     type: 'bar',
#                     data: {{
#                         labels: {json.dumps(labels)},
#                         datasets: [{{
#                             label: '{title}',
#                             data: {json.dumps(values)},
#                             backgroundColor: 'rgba(59, 130, 246, 0.6)',
#                             borderColor: 'rgba(59, 130, 246, 1)',
#                             borderWidth: 1,
#                             borderRadius: 4
#                         }}]
#                     }},
#                     options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true }} }} }}
#                 }});
#             }}, 100);
#         </script>
#     </div>
#     """

# @tool
# def create_line_chart_tool(title: str, labels: list[str], values: list[float]) -> str:
#     """Generates a Line Chart HTML/JS snippet. Pass in a title, a list of string labels, and a list of numeric values."""
#     chart_id = "chart_" + str(uuid.uuid4().hex)[:8]
#     return f"""
#     <div style="background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #eee;">
#         <h3 style="margin-top: 0; color: #111827; font-size: 1.1rem;">{title}</h3>
#         <canvas id="{chart_id}"></canvas>
#         <script>
#             setTimeout(() => {{
#                 new Chart(document.getElementById('{chart_id}'), {{
#                     type: 'line',
#                     data: {{
#                         labels: {json.dumps(labels)},
#                         datasets: [{{
#                             label: '{title}',
#                             data: {json.dumps(values)},
#                             fill: true,
#                             backgroundColor: 'rgba(16, 185, 129, 0.1)',
#                             borderColor: 'rgba(16, 185, 129, 1)',
#                             tension: 0.3,
#                             borderWidth: 2
#                         }}]
#                     }},
#                     options: {{ responsive: true, plugins: {{ legend: {{ display: true }} }}, scales: {{ y: {{ beginAtZero: true }} }} }}
#                 }});
#             }}, 100);
#         </script>
#     </div>
#     """

# @tool
# def assemble_crm_tool(components_html: list[str], layout_type: str, title: str = "CRM Agent Result") -> str:
#     """
#     Assembles generated components (tables, charts, widgets) into a finalized HTML string.
#     Set `layout_type` to "FULL_CRM" for a dashboard with a sidebar.
#     Set `layout_type` to "STANDALONE" if the user only wanted a single chart/table.
#     """
#     components = "".join(components_html)
    
#     if layout_type == "FULL_CRM":
#         return f"""
#         <!DOCTYPE html>
#         <html lang="en">
#         <head>
#             <meta charset="UTF-8">
#             <meta name="viewport" content="width=device-width, initial-scale=1.0">
#             <title>{title}</title>
#             <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
#             <style>
#                 body {{ font-family: 'Inter', sans-serif; background-color: #f3f4f6; margin: 0; padding: 0; }}
#                 .sidebar {{ width: 250px; background: #111827; color: white; height: 100vh; position: fixed; padding: 20px; box-sizing: border-box; }}
#                 .main-content {{ margin-left: 250px; padding: 30px; box-sizing: border-box; }}
#                 .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }}
#                 .header h1 {{ margin: 0; color: #111827; font-size: 1.8rem; font-weight: 800; }}
#                 .container {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 20px; }}
#             </style>
#         </head>
#         <body>
#             <div class="sidebar">
#                 <h2 style="font-weight: 800;">AI CRM<span style="color: #3b82f6;">.</span></h2>
#             </div>
#             <div class="main-content">
#                 <div class="header"><h1>{title}</h1></div>
#                 <div class="container">{components}</div>
#             </div>
#         </body>
#         </html>
#         """
#     else:
#         # STANDALONE output (e.g., just a single graph or table)
#         return f"""
#         <!DOCTYPE html>
#         <html lang="en">
#         <head>
#             <meta charset="UTF-8">
#             <meta name="viewport" content="width=device-width, initial-scale=1.0">
#             <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
#             <style>
#                 body {{ font-family: 'Inter', sans-serif; background-color: #ffffff; padding: 20px; margin: 0; display: flex; flex-direction: column; gap: 20px; }}
#             </style>
#         </head>
#         <body>
#             {components}
#         </body>
#         </html>
#         """
from langchain_core.tools import tool


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _build_response(
    ui_type: str,
    title: str,
    context: str,
    html: str,
    message: str = "",
) -> dict:
    return {
        "response_type": "html",
        "ui_type": _normalize_text(ui_type),
        "title": _normalize_text(title),
        "context": _normalize_text(context),
        "message": _normalize_text(message),
        "html": _normalize_text(html),
    }


def _ensure_full_html_document(title: str, html: str) -> str:
    """
    Ensures the returned HTML is a complete HTML document.

    If the provided html already contains a full HTML document, return it unchanged.
    Otherwise, wrap it in a minimal neutral HTML shell without imposing a visual theme.
    """
    html = _normalize_text(html)

    if not html:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{_normalize_text(title) or "UI Output"}</title>
</head>
<body>
</body>
</html>"""

    lowered = html.lower()
    if "<html" in lowered and "</html>" in lowered:
        return html

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{_normalize_text(title) or "UI Output"}</title>
</head>
<body>
{html}
</body>
</html>"""


@tool
def generate_crm_metric(
    title: str,
    context: str,
    html: str,
    message: str = "",
    standalone: bool = False,
) -> dict:
    """
    Generates a KPI or metric widget response.

    Use this tool when the requested UI is a metric, summary card, count,
    average, percentage, total, or other KPI-style output.

    If standalone=True, the provided html should represent a complete standalone
    page or will be wrapped into a minimal full HTML document.
    If standalone=False, the provided html is treated as a widget fragment that
    may later be combined into a larger dashboard.
    """
    final_html = _ensure_full_html_document(title, html) if standalone else _normalize_text(html)
    return _build_response("metric", title, context, final_html, message)


@tool
def generate_crm_table(
    title: str,
    context: str,
    html: str,
    message: str = "",
    standalone: bool = False,
) -> dict:
    """
    Generates a table widget response.

    Use this tool for detailed records, ranked lists, grouped tables,
    structured row/column outputs, or any table-based business view.

    If standalone=True, the provided html should represent a complete standalone
    page or will be wrapped into a minimal full HTML document.
    If standalone=False, the provided html is treated as a widget fragment that
    may later be combined into a larger dashboard.
    """
    final_html = _ensure_full_html_document(title, html) if standalone else _normalize_text(html)
    return _build_response("table", title, context, final_html, message)


@tool
def generate_crm_bar_chart(
    title: str,
    context: str,
    html: str,
    message: str = "",
    standalone: bool = False,
) -> dict:
    """
    Generates a bar chart widget response.

    Use this tool for category comparisons, grouped comparisons, or
    comparison-focused chart output.

    If standalone=True, the provided html should represent a complete standalone
    page or will be wrapped into a minimal full HTML document.
    If standalone=False, the provided html is treated as a widget fragment that
    may later be combined into a larger dashboard.
    """
    final_html = _ensure_full_html_document(title, html) if standalone else _normalize_text(html)
    return _build_response("bar_chart", title, context, final_html, message)


@tool
def generate_crm_line_chart(
    title: str,
    context: str,
    html: str,
    message: str = "",
    standalone: bool = False,
) -> dict:
    """
    Generates a line chart widget response.

    Use this tool for trend-like, sequential, ordered, or progression-based
    visualizations when such a view is honestly supported by the real data.

    If standalone=True, the provided html should represent a complete standalone
    page or will be wrapped into a minimal full HTML document.
    If standalone=False, the provided html is treated as a widget fragment that
    may later be combined into a larger dashboard.
    """
    final_html = _ensure_full_html_document(title, html) if standalone else _normalize_text(html)
    return _build_response("line_chart", title, context, final_html, message)


@tool
def generate_crm_pie_chart(
    title: str,
    context: str,
    html: str,
    message: str = "",
    standalone: bool = False,
) -> dict:
    """
    Generates a pie or donut chart widget response.

    Use this tool for distribution or share-of-total views where a pie-style
    presentation is appropriate and supported by the data.

    If standalone=True, the provided html should represent a complete standalone
    page or will be wrapped into a minimal full HTML document.
    If standalone=False, the provided html is treated as a widget fragment that
    may later be combined into a larger dashboard.
    """
    final_html = _ensure_full_html_document(title, html) if standalone else _normalize_text(html)
    return _build_response("pie_chart", title, context, final_html, message)


@tool
def generate_crm_scatter_plot(
    title: str,
    context: str,
    html: str,
    message: str = "",
    standalone: bool = False,
) -> dict:
    """
    Generates a scatter plot widget response.

    Use this tool for numeric-vs-numeric relationship analysis where a scatter
    chart is appropriate and supported by the data.

    If standalone=True, the provided html should represent a complete standalone
    page or will be wrapped into a minimal full HTML document.
    If standalone=False, the provided html is treated as a widget fragment that
    may later be combined into a larger dashboard.
    """
    final_html = _ensure_full_html_document(title, html) if standalone else _normalize_text(html)
    return _build_response("scatter_plot", title, context, final_html, message)


@tool
def generate_dashboard_html(
    title: str,
    context: str,
    html: str,
    message: str = "",
) -> dict:
    """
    Generates the final combined dashboard HTML response.

    Use this only when multiple widget fragments have already been created and
    need to be returned as one final full HTML document.

    This tool does not impose a fixed visual design language. The agent should
    provide the final layout, styling, colors, spacing, and interactions inside
    the supplied html.
    """
    final_html = _ensure_full_html_document(title, html)
    return _build_response("dashboard", title, context, final_html, message)
