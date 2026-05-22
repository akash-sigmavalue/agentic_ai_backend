from datetime import datetime
from math import isclose
from typing import Any

from sqlalchemy.orm import Session

from database.portfolio.models import Record
from database.portfolio.repositories import record_repository
from registry.portfolio.registry import get_section
from tools.portfolio.validation import parse_number


MISSING = object()


def split_record_by_category(record_data: dict, section: dict) -> tuple[dict, dict]:
    base_data = {}
    uploaded_derived = {}
    fields = {field["key"]: field for field in section.get("fields", [])}
    for key, value in (record_data or {}).items():
        field = fields.get(key)
        if field and field.get("category") == "derived":
            if value not in (None, ""):
                uploaded_derived[key] = value
            continue
        base_data[key] = value
    return base_data, uploaded_derived


def refresh_derived_records(db: Session, asset_ids: set[str] | None = None) -> None:
    rows = db.query(Record).filter(Record.section_key != "dashboard").all()
    if asset_ids:
        wanted = {str(asset_id) for asset_id in asset_ids if asset_id}
        rows_to_update = [row for row in rows if str(row.asset_id or "") in wanted]
    else:
        rows_to_update = rows

    sections = _records_by_section(rows)
    for row in rows_to_update:
        try:
            section = get_section(row.section_key)
        except Exception:
            continue
        updated_data, derived_audit = apply_derived_calculations(row, section, sections)
        record_repository.update_record(db, row, record_data=updated_data, derived_audit=derived_audit)
        print_derived_calculations(row.section_key, row.asset_id, derived_audit)


def apply_derived_calculations(row: Record, section: dict, sections: dict[str, list[dict]]) -> tuple[dict, dict]:
    record_data = dict(row.record_data or {})
    previous_audit = row.derived_audit or {}
    uploaded_values = dict(previous_audit.get("uploaded_values") or {})
    for field in section.get("fields", []):
        if field.get("category") == "derived" and record_data.get(field["key"]) not in (None, ""):
            if field["key"] not in previous_audit.get("fields", {}):
                uploaded_values.setdefault(field["key"], record_data.get(field["key"]))

    computed = compute_section_derived_values(section["section_key"], row.asset_id, sections, record_data)
    audit_fields = {}
    for field in section.get("fields", []):
        if field.get("category") != "derived":
            continue
        key = field["key"]
        uploaded_value = uploaded_values.get(key)
        result = computed.get(key)
        if result and result.get("value") not in (None, ""):
            computed_value = result["value"]
            inputs = result.get("inputs", {})
            status = "computed"
            reason = "System calculated the derived value."
            if uploaded_value not in (None, ""):
                status = "verified" if _values_equivalent(uploaded_value, computed_value) else "mismatch"
                reason = "Uploaded derived value matches the system calculation." if status == "verified" else "Uploaded derived value differs from the system calculation."
            if uploaded_value not in (None, "") and _is_zero_value(computed_value) and not _values_equivalent(uploaded_value, computed_value):
                record_data[key] = uploaded_value
                status = "unverified"
                reason = "Using uploaded derived value because the system calculation returned 0 and may be missing required input data."
            else:
                record_data[key] = computed_value
            display_value = uploaded_value if status == "unverified" and uploaded_value not in (None, "") else computed_value
            audit_fields[key] = {
                "field_label": field.get("label", key),
                "uploaded_value": uploaded_value,
                "computed_value": computed_value,
                "display_value": display_value,
                "status": status,
                "reason": reason,
                "formula": result.get("formula", ""),
                "inputs": inputs,
                "calculation_inputs": _calculation_inputs(inputs),
            }
        elif uploaded_value not in (None, ""):
            inputs = result.get("inputs", {}) if result else {}
            record_data[key] = uploaded_value
            audit_fields[key] = {
                "field_label": field.get("label", key),
                "uploaded_value": uploaded_value,
                "computed_value": None,
                "display_value": uploaded_value,
                "status": "unverified",
                "reason": "Using uploaded derived value because the system could not calculate this field from available input data.",
                "formula": result.get("formula", "") if result else "",
                "inputs": inputs,
                "calculation_inputs": _calculation_inputs(inputs),
            }

    derived_values = _derived_values_summary(audit_fields)
    return record_data, {"uploaded_values": uploaded_values, "fields": audit_fields, "derived_values": derived_values}


def compute_section_derived_values(section_key: str, asset_id: str | None, sections: dict[str, list[dict]], current_data: dict | None = None) -> dict:
    ctx = AssetContext(sections, asset_id, current_section=section_key, current_data=current_data or {})
    current = current_data or {}

    carpet_area = ctx.number("physical_details", "carpetArea")
    leasable_area = ctx.number("physical_details", "leasableArea")
    market_rate = ctx.number("market_benchmarking", "marketRate")
    acquisition_cost = ctx.number("financial_value", "acquisitionCost")
    current_market_value = ctx.number("financial_value", "currentMarketValue") or (market_rate * carpet_area if market_rate and carpet_area else 0)
    rental_income = ctx.number("revenue_income", "rentalIncome")
    occupancy_pct = ctx.number("occupancy_leasing", "occupancy") / 100
    occupancy_income = rental_income * occupancy_pct if rental_income and occupancy_pct else 0
    other_income = ctx.number("revenue_income", "otherIncome")
    maintenance = ctx.number("expenses", "maintenanceCost")
    property_tax = ctx.number("expenses", "propertyTax")
    insurance = ctx.number("expenses", "insurance")
    utilities = ctx.number("expenses", "utilities")
    repairs = ctx.number("expenses", "repairs")
    opex = maintenance + property_tax + insurance + utilities + repairs
    noi = occupancy_income + other_income - opex
    emi = _pmt(ctx.number("loan_debt", "interestRate"), ctx.number("loan_debt", "loanTenure") or 120, ctx.number("loan_debt", "loanAmount"))
    annual_debt_service = emi * 12 if emi else 0
    outstanding = ctx.number("loan_debt", "outstandingPrincipal")
    dscr = noi / annual_debt_service if annual_debt_service else 0
    ltv = outstanding / current_market_value * 100 if current_market_value and outstanding else 0
    red_flag_score = _average([
        _risk_weight(ctx.value("risk_fields", "legalRisk")),
        _risk_weight(ctx.value("risk_fields", "marketRisk")),
        _risk_weight(ctx.value("risk_fields", "tenantRisk")),
        _risk_weight(ctx.value("risk_fields", "valuationRisk")),
        _risk_weight(ctx.value("risk_fields", "liquidityRisk")),
        _risk_weight(ctx.value("risk_fields", "regulatoryRisk")),
    ])

    def result(value: Any, formula: str, inputs: dict[str, Any]) -> dict:
        return {"value": _clean_computed(value), "formula": formula, "inputs": inputs}

    values = {}
    if section_key == "esg_data":
        values["emissions"] = result(ctx.number("esg_data", "energyConsumption") * 0.0007, "Energy Consumption * 0.0007", {"energyConsumption": ctx.value("esg_data", "energyConsumption")})
        approvals = str(ctx.value("ownership_legal", "approvals") or "").lower()
        values["complianceStatus"] = result("Compliant" if approvals in {"approved", "partly approved"} else "Review", "Approvals in Approved/Partly Approved", {"approvals": ctx.value("ownership_legal", "approvals")})
        tenant_safety = ctx.number("esg_data", "tenantHealthSafetyScore")
        climate = _risk_weight(ctx.value("esg_data", "climateRisk"))
        green = 70 if str(ctx.value("esg_data", "greenCertification") or "").lower() == "none" else 25
        values["esgRiskScore"] = result(_average([100 - tenant_safety, climate, green]), "Average(100 - Tenant Health/Safety Score, Climate Risk Score, Green Certification Risk Score)", {"tenantHealthSafetyScore": tenant_safety, "climateRiskScore": climate, "greenCertificationRiskScore": green})
    elif section_key == "financial_value":
        values["currentMarketValue"] = result(market_rate * carpet_area, "Market Rate * Carpet Area", {"marketRate": market_rate, "carpetArea": carpet_area}) if market_rate and carpet_area else {}
        values["appreciationDepreciation"] = result((current_market_value - acquisition_cost) / acquisition_cost * 100, "(Current Market Value - Acquisition Cost) / Acquisition Cost * 100", {"currentMarketValue": current_market_value, "acquisitionCost": acquisition_cost}) if current_market_value and acquisition_cost else {}
    elif section_key == "revenue_income":
        values["occupancyIncome"] = result(occupancy_income, "Rental Income * Occupancy %", {"rentalIncome": rental_income, "occupancy": ctx.value("occupancy_leasing", "occupancy")}) if rental_income and occupancy_pct else {}
        values["totalIncome"] = result(rental_income + other_income, "Rental Income + Other Income", {"rentalIncome": rental_income, "otherIncome": other_income})
    elif section_key == "expenses":
        values["opex"] = result(opex, "Maintenance Cost + Property Tax + Insurance + Utilities + Repairs", {"maintenanceCost": maintenance, "propertyTax": property_tax, "insurance": insurance, "utilities": utilities, "repairs": repairs})
    elif section_key == "occupancy_leasing":
        values["vacantArea"] = result(leasable_area * (1 - occupancy_pct), "Leasable Area * (1 - Occupancy %)", {"leasableArea": leasable_area, "occupancy": ctx.value("occupancy_leasing", "occupancy")}) if leasable_area and occupancy_pct else {}
    elif section_key == "loan_debt":
        values["emi"] = result(emi, "PMT(Interest Rate / 12, Loan Tenure, Loan Amount)", {"interestRate": ctx.value("loan_debt", "interestRate"), "loanTenure": ctx.value("loan_debt", "loanTenure") or 120, "loanAmount": ctx.value("loan_debt", "loanAmount")}) if emi else {}
        values["ltv"] = result(ltv, "Outstanding Principal / Current Market Value * 100", {"outstandingPrincipal": outstanding, "currentMarketValue": current_market_value}) if ltv else {}
        values["dscr"] = result(dscr, "NOI / (EMI * 12)", {"noi": noi, "emi": emi}) if dscr else {}
    elif section_key == "risk_fields":
        values["redFlagScore"] = result(
            red_flag_score,
            "Average risk score where High=90, Medium=60, Low=25",
            {
                "legalRisk": ctx.value("risk_fields", "legalRisk"),
                "marketRisk": ctx.value("risk_fields", "marketRisk"),
                "tenantRisk": ctx.value("risk_fields", "tenantRisk"),
                "valuationRisk": ctx.value("risk_fields", "valuationRisk"),
                "liquidityRisk": ctx.value("risk_fields", "liquidityRisk"),
                "regulatoryRisk": ctx.value("risk_fields", "regulatoryRisk"),
            },
        )
    elif section_key == "performance_metrics":
        equity = acquisition_cost - ctx.number("loan_debt", "loanAmount")
        values["noi"] = result(noi, "Occupancy Income + Other Income - Opex", {"occupancyIncome": occupancy_income, "otherIncome": other_income, "opex": opex})
        values["ebitda"] = result(noi - insurance, "NOI - Insurance", {"noi": noi, "insurance": insurance})
        values["capRate"] = result(noi / current_market_value * 100, "NOI / Current Market Value * 100", {"noi": noi, "currentMarketValue": current_market_value}) if current_market_value else {}
        values["roi"] = result(noi / acquisition_cost * 100, "NOI / Acquisition Cost * 100", {"noi": noi, "acquisitionCost": acquisition_cost}) if acquisition_cost else {}
        values["yieldPercentage"] = result(rental_income / current_market_value * 100, "Rental Income / Current Market Value * 100", {"rentalIncome": rental_income, "currentMarketValue": current_market_value}) if current_market_value else {}
        values["paybackPeriod"] = result(acquisition_cost / noi, "Acquisition Cost / NOI", {"acquisitionCost": acquisition_cost, "noi": noi}) if noi else {}
        values["cashOnCashReturn"] = result((noi - annual_debt_service) / equity * 100, "(NOI - EMI * 12) / (Acquisition Cost - Loan Amount) * 100", {"noi": noi, "annualDebtService": annual_debt_service, "equity": equity}) if equity else {}
    elif section_key == "cash_flow_analysis":
        capex_reserve = _to_number(current.get("capexReserve"))
        opening_value = _to_number(current.get("openingValue")) or current_market_value
        free_cash_flow = noi - annual_debt_service - capex_reserve
        values["grossRentalIncome"] = result(rental_income, "Rental Income", {"rentalIncome": rental_income})
        values["totalGrossIncome"] = result(rental_income + other_income, "Gross Rental Income + Other Income", {"grossRentalIncome": rental_income, "otherIncome": other_income})
        values["operatingExpenses"] = result(opex, "Maintenance Cost + Property Tax + Insurance + Utilities + Repairs", {"opex": opex})
        values["noi"] = result(noi, "Total Gross Income - Operating Expenses", {"totalGrossIncome": rental_income + other_income, "operatingExpenses": opex})
        values["annualDebtService"] = result(annual_debt_service, "EMI * 12", {"emi": emi})
        values["netCashFlowAfterDebt"] = result(noi - annual_debt_service, "NOI - Annual Debt Service", {"noi": noi, "annualDebtService": annual_debt_service})
        values["freeCashFlow"] = result(free_cash_flow, "Net Cash Flow After Debt - Capex Reserve", {"netCashFlowAfterDebt": noi - annual_debt_service, "capexReserve": capex_reserve})
        values["dscr"] = result(dscr, "NOI / Annual Debt Service", {"noi": noi, "annualDebtService": annual_debt_service}) if dscr else {}
        values["cashYield"] = result(free_cash_flow / opening_value * 100, "Free Cash Flow / Opening Value * 100", {"freeCashFlow": free_cash_flow, "openingValue": opening_value}) if opening_value else {}
    elif section_key == "workflow_actions":
        alerts = []
        if red_flag_score > 70:
            alerts.append("High risk")
        if ltv and ltv > 75:
            alerts.append("High LTV")
        if dscr and dscr < 1.25:
            alerts.append("Low DSCR")
        if str(ctx.value("ownership_legal", "approvals") or "").lower() not in {"approved", "partly approved"}:
            alerts.append("Compliance review")
        values["alerts"] = result("; ".join(alerts), "Text alerts from risk, LTV, DSCR and compliance thresholds", {"redFlagScore": red_flag_score, "ltv": ltv, "dscr": dscr, "approvals": ctx.value("ownership_legal", "approvals")})
    elif section_key == "exit_strategy":
        acquisition_date = ctx.value("exit_strategy", "acquisitionDate")
        expected_sale_date = ctx.value("exit_strategy", "expectedSaleDate")
        target_exit_value = current_market_value * 1.25 if current_market_value else 0
        years = _years_between(acquisition_date, expected_sale_date)
        exit_irr = ((target_exit_value / acquisition_cost) ** (1 / years) - 1) * 100 if target_exit_value and acquisition_cost and years else 0
        values["exitIrr"] = result(exit_irr, "[(Target Exit Value / Acquisition Cost) ^ (1 / Holding Period Years) - 1] * 100", {"targetExitValue": target_exit_value, "acquisitionCost": acquisition_cost, "holdingPeriodYears": years}) if exit_irr else {}

    return values


class AssetContext:
    def __init__(self, sections: dict[str, list[dict]], asset_id: str | None, *, current_section: str, current_data: dict):
        self.sections = sections
        self.asset_id = str(asset_id or "").strip()
        self.current_section = current_section
        self.current_data = current_data

    def record(self, section_key: str) -> dict:
        if section_key == self.current_section:
            return self.current_data
        records = self.sections.get(section_key, [])
        if self.asset_id:
            for record in records:
                if str(record.get("assetId") or "").strip() == self.asset_id:
                    return record
        return records[0] if records else {}

    def value(self, section_key: str, field_key: str) -> Any:
        return self.record(section_key).get(field_key)

    def number(self, section_key: str, field_key: str) -> float:
        return _to_number(self.value(section_key, field_key))


def _records_by_section(rows: list[Record]) -> dict[str, list[dict]]:
    sections = {}
    for row in rows:
        data = dict(row.record_data or {})
        data.update((row.derived_audit or {}).get("uploaded_values") or {})
        sections.setdefault(row.section_key, []).append(data)
    return sections


def _calculation_inputs(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": key, "value": value} for key, value in (inputs or {}).items()]


def _derived_values_summary(audit_fields: dict[str, dict]) -> list[dict[str, Any]]:
    return [
        {
            "field_key": field_key,
            "field_label": details.get("field_label", field_key),
            "computed_value": details.get("computed_value"),
            "uploaded_value": details.get("uploaded_value"),
            "display_value": details.get("display_value", details.get("uploaded_value") if details.get("status") == "unverified" else details.get("computed_value")),
            "status": details.get("status"),
            "reason": details.get("reason", ""),
            "formula": details.get("formula", ""),
            "calculation_inputs": details.get("calculation_inputs", []),
        }
        for field_key, details in audit_fields.items()
    ]


def derived_audit_with_summary(derived_audit: dict | None) -> dict:
    audit = dict(derived_audit or {})
    audit.setdefault("uploaded_values", {})
    audit.setdefault("fields", {})
    audit["derived_values"] = audit.get("derived_values") or _derived_values_summary(audit.get("fields", {}))
    return audit


def print_derived_calculations(section_key: str, asset_id: str | None, derived_audit: dict) -> None:
    derived_values = derived_audit_with_summary(derived_audit).get("derived_values", [])
    if not derived_values:
        return

    print(f"[derived] section={section_key} asset_id={asset_id or ''}")
    for item in derived_values:
        value = item.get("display_value")
        if value in (None, ""):
            value = item.get("computed_value")
        if value in (None, ""):
            value = item.get("uploaded_value")
        inputs = ", ".join(
            f"{entry.get('name')}={entry.get('value')}"
            for entry in item.get("calculation_inputs", [])
        ) or "none"
        print(
            f"  - {item.get('field_label')} ({item.get('field_key')}): "
            f"value={value} status={item.get('status')} formula={item.get('formula')} inputs=[{inputs}]"
        )


def _to_number(value: Any) -> float:
    parsed = parse_number(value)
    return float(parsed or 0)


def _clean_computed(value: Any) -> Any:
    if isinstance(value, float):
        if isclose(value, round(value), abs_tol=0.000001):
            return int(round(value))
        return round(value, 4)
    return value


def _values_equivalent(left: Any, right: Any) -> bool:
    left_number = parse_number(left)
    right_number = parse_number(right)
    if left_number is not None and right_number is not None:
        return isclose(float(left_number), float(right_number), rel_tol=0.0001, abs_tol=0.01)
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def _is_zero_value(value: Any) -> bool:
    number = parse_number(value)
    return number is not None and isclose(float(number), 0.0, abs_tol=0.000001)


def _risk_weight(value: Any) -> int:
    text = str(value or "").lower()
    if "high" in text:
        return 90
    if "medium" in text:
        return 60
    if "low" in text:
        return 25
    return 50


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0


def _pmt(annual_rate: Any, months: Any, principal: Any) -> float:
    principal_value = _to_number(principal)
    month_count = int(_to_number(months))
    annual_rate_value = _to_number(annual_rate)
    if not principal_value or not month_count:
        return 0
    rate = annual_rate_value / 100 / 12
    if not rate:
        return principal_value / month_count
    return (principal_value * rate) / (1 - (1 + rate) ** -month_count)


def _years_between(start: Any, end: Any) -> float:
    try:
        start_date = datetime.fromisoformat(str(start)).date()
        end_date = datetime.fromisoformat(str(end)).date()
    except Exception:
        return 0
    days = (end_date - start_date).days
    return days / 365 if days > 0 else 0
