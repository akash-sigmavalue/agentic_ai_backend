from langchain_core.tools import tool


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _build_response(
    ui_type: str,
    title: str,
    context: str,
    jsx: str,
    message: str = "",
) -> dict:
    normalized_jsx = _normalize_text(jsx)
    return {
        "response_type": "jsx",
        "ui_type": _normalize_text(ui_type),
        "title": _normalize_text(title),
        "context": _normalize_text(context),
        "message": _normalize_text(message),
        "jsx": normalized_jsx,
        "html": normalized_jsx,
    }


def _ensure_raw_jsx_code(title: str, jsx: str) -> str:
    """
    Ensures the returned value is treated as raw JSX code.

    This function intentionally does not wrap or transform the code,
    because the agent should return valid JSX/JS module content directly.
    """
    return _normalize_text(jsx)


@tool
def generate_metric_widget(
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
    final_jsx = _ensure_raw_jsx_code(title, html) if standalone else _normalize_text(html)
    return _build_response("metric", title, context, final_jsx, message)


@tool
def generate_table_widget(
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
    final_jsx = _ensure_raw_jsx_code(title, html) if standalone else _normalize_text(html)
    return _build_response("table", title, context, final_jsx, message)


@tool
def generate_bar_chart_widget(
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
    final_jsx = _ensure_raw_jsx_code(title, html) if standalone else _normalize_text(html)
    return _build_response("bar_chart", title, context, final_jsx, message)


@tool
def generate_line_chart_widget(
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
    final_jsx = _ensure_raw_jsx_code(title, html) if standalone else _normalize_text(html)
    return _build_response("line_chart", title, context, final_jsx, message)


@tool
def generate_pie_chart_widget(
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
    final_jsx = _ensure_raw_jsx_code(title, html) if standalone else _normalize_text(html)
    return _build_response("pie_chart", title, context, final_jsx, message)


@tool
def generate_scatter_plot_widget(
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
    final_jsx = _ensure_raw_jsx_code(title, html) if standalone else _normalize_text(html)
    return _build_response("scatter_plot", title, context, final_jsx, message)


@tool
def generate_dashboard_html(
    title: str,
    context: str,
    html: str,
    message: str = "",
) -> dict:
    """
    Generates the final combined dashboard JSX response.

    Use this only when multiple widget fragments have already been created and
    need to be returned as one final JSX component or module string.

    This tool does not impose a fixed visual design language. The agent should
    provide the final layout, styling, colors, spacing, and interactions inside
    the supplied code.
    """
    final_jsx = _ensure_raw_jsx_code(title, html)
    return _build_response("dashboard", title, context, final_jsx, message)
