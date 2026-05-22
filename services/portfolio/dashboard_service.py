from database.portfolio.models import Record


DASHBOARD_COLORS = {
    "acquisitionCost": "#2563eb",
    "currentMarketValue": "#16a34a",
    "bookValue": "#f59e0b",
    "rentalIncome": "#7c3aed",
    "occupancyIncome": "#06b6d4",
    "otherIncome": "#f97316",
    "collectionEfficiency": "#dc2626",
    "maintenance": "#0f766e",
    "propertyTax": "#9333ea",
    "insurance": "#0891b2",
    "utilities": "#ca8a04",
    "repairs": "#be123c",
    "capex": "#475569",
    "opex": "#ea580c",
    "emi": "#1d4ed8",
}


DASHBOARD_STRUCTURE = [
    {
        "id": "asset_identity",
        "title": "1. Asset Identity",
        "description": "Linked asset-wise using Asset ID from Asset Identity section. After field edits, click Save All.",
        "type": "table",
        "table": {
            "columns": [
                {"key": "assetId", "label": "Asset ID"},
                {"key": "propertyName", "label": "Property Name"},
                {"key": "micromarket", "label": "Micromarket"},
                {"key": "city", "label": "City"},
            ]
        },
    },
    {
        "id": "valuation_snapshot",
        "title": "2. Valuation Snapshot",
        "description": "Asset-wise Acquisition Cost, Current Market Value, Book Value and appreciation/depreciation.",
        "type": "chart_table",
        "chart": {
            "kind": "bar",
            "x_axis": "assetId",
            "height": 260,
            "series": [
                {"key": "acquisitionCost", "label": "Acquisition Cost", "color": DASHBOARD_COLORS["acquisitionCost"]},
                {"key": "currentMarketValue", "label": "Current Market Value", "color": DASHBOARD_COLORS["currentMarketValue"]},
                {"key": "bookValue", "label": "Book Value", "color": DASHBOARD_COLORS["bookValue"]},
            ],
        },
        "table": {
            "columns": [
                {"key": "assetId", "label": "Asset ID"},
                {"key": "valuationDate", "label": "Valuation Date"},
                {"key": "valuationMethod", "label": "Valuation Method"},
                {"key": "appreciationLabel", "label": "Appreciation / Depreciation"},
            ]
        },
    },
    {
        "id": "revenue_income",
        "title": "3. Revenue & Income",
        "description": "Rental income, occupancy income, other income, escalation clause and collection efficiency.",
        "type": "chart_table",
        "chart": {
            "kind": "composed",
            "x_axis": "assetId",
            "height": 260,
            "series": [
                {"key": "rentalIncome", "label": "Rental Income", "color": DASHBOARD_COLORS["rentalIncome"], "mark": "bar"},
                {"key": "occupancyIncome", "label": "Occupancy Income", "color": DASHBOARD_COLORS["occupancyIncome"], "mark": "bar"},
                {"key": "otherIncome", "label": "Other Income", "color": DASHBOARD_COLORS["otherIncome"], "mark": "bar"},
                {"key": "collectionEfficiency", "label": "Collection Efficiency %", "color": DASHBOARD_COLORS["collectionEfficiency"], "mark": "line", "stroke_width": 3},
            ],
        },
        "table": {
            "columns": [
                {"key": "assetId", "label": "Asset ID"},
                {"key": "escalationClause", "label": "Escalation Clause"},
                {"key": "collectionEfficiencyLabel", "label": "Collection Efficiency"},
            ]
        },
    },
    {
        "id": "expenses",
        "title": "4. Expenses",
        "description": "Maintenance cost, property tax, insurance, utilities, repairs, capex and opex.",
        "type": "chart",
        "chart": {
            "kind": "bar",
            "x_axis": "assetId",
            "height": 260,
            "series": [
                {"key": "maintenance", "label": "Maintenance", "color": DASHBOARD_COLORS["maintenance"]},
                {"key": "propertyTax", "label": "Property Tax", "color": DASHBOARD_COLORS["propertyTax"]},
                {"key": "insurance", "label": "Insurance", "color": DASHBOARD_COLORS["insurance"]},
                {"key": "utilities", "label": "Utilities", "color": DASHBOARD_COLORS["utilities"]},
                {"key": "repairs", "label": "Repairs", "color": DASHBOARD_COLORS["repairs"]},
                {"key": "capex", "label": "Capex", "color": DASHBOARD_COLORS["capex"]},
                {"key": "opex", "label": "Opex", "color": DASHBOARD_COLORS["opex"]},
            ],
        },
    },
    {
        "id": "emi",
        "title": "5. EMI",
        "description": "Asset-wise monthly EMI from Loan / Debt section.",
        "type": "chart",
        "chart": {
            "kind": "bar",
            "x_axis": "assetId",
            "height": 240,
            "series": [
                {"key": "emi", "label": "EMI", "color": DASHBOARD_COLORS["emi"]},
            ],
        },
    },
]


def to_number(value):
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("₹", "").replace("%", "").strip())
    except Exception:
        return 0.0


def pmt(annual_rate, months, principal):
    principal = to_number(principal)
    months = int(to_number(months) or 0)
    annual_rate = to_number(annual_rate)
    if not principal or not months:
        return 0.0
    r = annual_rate / 100 / 12
    if not r:
        return principal / months
    return (principal * r) / (1 - (1 + r) ** -months)


def _ordered_records(db):
    return (
        db.query(Record)
        .filter(Record.section_key != "dashboard")
        .order_by(Record.section_key.asc(), Record.source_row_number.is_(None), Record.source_row_number.asc(), Record.id.asc())
        .all()
    )


def records_by_section(db):
    grouped = {}
    for row in _ordered_records(db):
        grouped.setdefault(row.section_key, []).append(row.record_data or {})
    return grouped


def _record_asset_id(record):
    return str(record.get("assetId") or "").strip()


def _record_for_asset(records, asset_id, row_index=0):
    wanted = str(asset_id or "").strip()
    if wanted:
        for record in records:
            if _record_asset_id(record) == wanted:
                return record
    if row_index < len(records):
        return records[row_index]
    return {}


def _asset_rows(sections):
    assets = sections.get("asset_identity", [])
    if assets:
        return assets

    seen = set()
    inferred = []
    for records in sections.values():
        for record in records:
            asset_id = _record_asset_id(record)
            if asset_id and asset_id not in seen:
                seen.add(asset_id)
                inferred.append({"assetId": asset_id})
    return inferred


def compute_for_asset(asset_id, sections, row_index=0, asset=None):
    asset = asset or _record_for_asset(sections.get("asset_identity", []), asset_id, row_index)
    physical = _record_for_asset(sections.get("physical_details", []), asset_id, row_index)
    financial = _record_for_asset(sections.get("financial_value", []), asset_id, row_index)
    revenue = _record_for_asset(sections.get("revenue_income", []), asset_id, row_index)
    expenses = _record_for_asset(sections.get("expenses", []), asset_id, row_index)
    occupancy = _record_for_asset(sections.get("occupancy_leasing", []), asset_id, row_index)
    loan = _record_for_asset(sections.get("loan_debt", []), asset_id, row_index)
    market = _record_for_asset(sections.get("market_benchmarking", []), asset_id, row_index)

    carpet = to_number(physical.get("carpetArea"))
    market_rate = to_number(market.get("marketRate"))
    current_value = to_number(financial.get("currentMarketValue")) or (market_rate * carpet if market_rate and carpet else 0)
    acquisition = to_number(financial.get("acquisitionCost"))
    book = to_number(financial.get("bookValue"))
    rental = to_number(revenue.get("rentalIncome"))
    other_income = to_number(revenue.get("otherIncome"))
    occ_pct = to_number(occupancy.get("occupancy") or revenue.get("collectionEfficiency"))
    occupancy_income = to_number(revenue.get("occupancyIncome")) or (rental * (occ_pct / 100) if occ_pct else 0)
    maintenance = to_number(expenses.get("maintenanceCost"))
    tax = to_number(expenses.get("propertyTax"))
    insurance = to_number(expenses.get("insurance"))
    utilities = to_number(expenses.get("utilities"))
    repairs = to_number(expenses.get("repairs"))
    capex = to_number(expenses.get("capex"))
    opex = to_number(expenses.get("opex")) or maintenance + tax + insurance + utilities + repairs
    emi = to_number(loan.get("emi")) or pmt(loan.get("interestRate"), loan.get("loanTenure"), loan.get("loanAmount"))
    outstanding = to_number(loan.get("outstandingPrincipal"))
    total_income = to_number(revenue.get("totalIncome")) or rental + other_income
    noi = total_income - opex
    ltv = (outstanding / current_value * 100) if current_value and outstanding else 0
    dscr = (noi / (emi * 12)) if emi else 0
    appreciation = ((current_value - acquisition) / acquisition * 100) if acquisition and current_value else to_number(financial.get("appreciationDepreciation"))
    cap_rate = (noi / current_value * 100) if current_value else 0
    roi = (noi / acquisition * 100) if acquisition else 0

    return {
        "dashboardId": f"DSH-{row_index + 1:03d}",
        "assetId": asset_id,
        "propertyName": asset.get("propertyName", ""),
        "micromarket": asset.get("micromarket", ""),
        "city": asset.get("city", ""),
        "acquisitionCost": acquisition,
        "currentMarketValue": current_value,
        "bookValue": book,
        "valuationDate": financial.get("valuationDate", ""),
        "valuationMethod": financial.get("valuationMethod", ""),
        "appreciation": appreciation,
        "appreciationLabel": f"{appreciation:.2f}%" if appreciation else "",
        "appreciationDepreciation": appreciation,
        "rentalIncome": rental,
        "occupancyIncome": occupancy_income,
        "otherIncome": other_income,
        "totalIncome": total_income,
        "escalationClause": revenue.get("escalationClause", ""),
        "collectionEfficiency": to_number(revenue.get("collectionEfficiency")),
        "collectionEfficiencyLabel": f"{to_number(revenue.get('collectionEfficiency')):.2f}%" if to_number(revenue.get("collectionEfficiency")) else "",
        "maintenance": maintenance,
        "maintenanceCost": maintenance,
        "propertyTax": tax,
        "insurance": insurance,
        "utilities": utilities,
        "repairs": repairs,
        "capex": capex,
        "opex": opex,
        "emi": emi,
        "ltv": ltv,
        "dscr": dscr,
        "noi": noi,
        "ebitda": noi - insurance,
        "capRate": cap_rate,
        "roi": roi,
    }


def dashboard_rows(db):
    sections = records_by_section(db)
    rows = []
    for index, asset in enumerate(_asset_rows(sections)):
        asset_id = _record_asset_id(asset) or f"AST-{index + 1:03d}"
        rows.append(compute_for_asset(asset_id, sections, index, asset))
    return rows


def dashboard(db):
    rows = dashboard_rows(db)
    return {
        "rows": rows,
        "chart_rows": rows,
        "structure": DASHBOARD_STRUCTURE,
        "colors": DASHBOARD_COLORS,
        "summary": {
            "assetCount": len(rows),
            "totalMarketValue": sum(r["currentMarketValue"] for r in rows),
            "totalIncome": sum(r["totalIncome"] for r in rows),
            "totalEmi": sum(r["emi"] for r in rows),
        },
    }
