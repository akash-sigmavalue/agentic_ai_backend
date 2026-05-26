from database.portfolio.models import PortfolioFlatRecord


IMPORTANT_COLUMN_MEANINGS = {
    "asset_id": "Unique asset identifier linking all portfolio data.",
    "property_name": "Property, project, building, or asset name.",
    "asset_type": "Asset category such as office, retail, residential, land, or warehouse.",
    "city": "City where the asset is located.",
    "micromarket": "Real estate submarket, locality, neighbourhood, or location.",
    "current_market_value": "Current valuation or fair market value.",
    "acquisition_cost": "Purchase or acquisition cost.",
    "book_value": "Book value.",
    "rental_income": "Annual rental income.",
    "occupancy_income": "Annual occupancy-linked income.",
    "other_income": "Other annual income.",
    "total_income": "Total annual income.",
    "maintenance_cost": "Annual maintenance or facility cost.",
    "property_tax": "Annual property tax.",
    "insurance": "Annual insurance cost.",
    "utilities": "Annual utility cost.",
    "repairs": "Annual repair cost.",
    "capex": "Capital expenditure.",
    "opex": "Operating expense.",
    "occupancy": "Occupancy percentage.",
    "tenant_name": "Tenant, lessee, occupier, or tenant company.",
    "loan_amount": "Sanctioned loan or debt amount.",
    "lender": "Bank, financier, NBFC, or lending institution.",
    "interest_rate": "Loan interest rate percentage.",
    "loan_tenure": "Loan duration or term.",
    "emi": "Monthly loan installment.",
    "outstanding_principal": "Outstanding loan principal.",
    "ltv": "Loan-to-value percentage.",
    "dscr": "Debt service coverage ratio.",
    "legal_risk": "Legal risk descriptor.",
    "market_risk": "Market risk descriptor.",
    "tenant_risk": "Tenant risk descriptor.",
    "valuation_risk": "Valuation risk descriptor.",
    "liquidity_risk": "Liquidity risk descriptor.",
    "regulatory_risk": "Regulatory risk descriptor.",
    "red_flag_score": "Numeric red flag score.",
    "noi": "Net operating income.",
    "ebitda": "EBITDA.",
    "cap_rate": "Capitalization rate percentage.",
    "irr": "Internal rate of return percentage.",
    "roi": "Return on investment percentage.",
    "yield_percentage": "Yield percentage.",
    "market_rent": "Market rent.",
    "market_rate": "Market sale or valuation rate.",
    "vacancy_rate": "Vacancy percentage.",
    "assigned_user": "Workflow owner or assigned user.",
    "task_status": "Workflow task status.",
    "approval_status": "Approval status.",
}


def portfolio_query_schema() -> str:
    lines = [
        "Use only the following table for portfolio chat queries.",
        "",
        "portfolio_flat_records:",
    ]
    for column in PortfolioFlatRecord.__table__.columns:
        meaning = IMPORTANT_COLUMN_MEANINGS.get(column.name, "")
        type_name = column.type.compile()
        if meaning:
            lines.append(f"  {column.name} ({type_name}): {meaning}")
        else:
            lines.append(f"  {column.name} ({type_name})")
    return "\n".join(lines)


PORTFOLIO_QUERY_SCHEMA = portfolio_query_schema()
