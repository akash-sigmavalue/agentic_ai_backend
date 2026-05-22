from copy import deepcopy


def f(key, label, category="raw", data_type="text", required=False, aliases=None):
    return {"key": key, "label": label, "category": category, "data_type": data_type, "required": required, "aliases": aliases or []}


def section(key, label, id_field, fields, upload_enabled=True, master_label=None):
    return {
        "section_key": key,
        "label": label,
        "master_label": master_label or label,
        "id_field": id_field,
        "upload_enabled": upload_enabled,
        "fields": fields,
    }


SECTIONS = {
    "dashboard": section("dashboard", "Dashboard", "dashboardId", [
        f("dashboardId", "Dashboard ID", "system"),
        f("assetId", "Asset ID", "system", required=True),
        f("propertyName", "Property Name", "derived"),
        f("micromarket", "Micromarket", "derived"),
        f("city", "City", "derived"),
        f("acquisitionCost", "Acquisition Cost (₹)", "derived", "currency"),
        f("currentMarketValue", "Current Market Value (₹)", "raw", "currency"),
        f("bookValue", "Book Value (₹)", "derived", "currency"),
        f("valuationDate", "Valuation Date", "derived", "date"),
        f("valuationMethod", "Valuation Method", "derived"),
        f("appreciationDepreciation", "Appreciation / Depreciation", "derived", "percentage"),
        f("rentalIncome", "Rental Income (₹/yr)", "derived", "currency"),
        f("occupancyIncome", "Occupancy Income (₹/yr)", "derived", "currency"),
        f("otherIncome", "Other Income (₹/yr)", "raw", "currency"),
        f("escalationClause", "Escalation Clause", "derived", "percentage"),
        f("collectionEfficiency", "Collection Efficiency (%)", "derived", "percentage"),
        f("maintenanceCost", "Maintenance Cost (₹/yr)", "derived", "currency"),
        f("propertyTax", "Property Tax (₹/yr)", "derived", "currency"),
        f("insurance", "Insurance (₹/yr)", "derived", "currency"),
        f("utilities", "Utilities (₹/yr)", "derived", "currency"),
        f("repairs", "Repairs (₹/yr)", "derived", "currency"),
        f("capex", "Capex (₹)", "derived", "currency"),
        f("opex", "Opex (₹/yr)", "derived", "currency"),
        f("emi", "EMI (₹/month)", "derived", "currency"),
    ], upload_enabled=False, master_label="Portfolio Dashboard"),
    "asset_identity": section("asset_identity", "Asset Identity", "assetId", [
        f("assetId", "Asset ID", "system", required=True, aliases=["property id", "asset code"]), f("propertyName", "Property Name", aliases=["building name", "project name"]), f("assetType", "Asset Type"), f("ownershipType", "Ownership Type"), f("address", "Address"), f("city", "City"), f("micromarket", "Micromarket", aliases=["locality", "location"]), f("latitude", "Latitude", data_type="number"), f("longitude", "Longitude", data_type="number")
    ], master_label="Asset Identity Master"),
    "ownership_legal": section("ownership_legal", "Ownership & Legal", "assetId", [f("assetId", "Asset ID", "system", required=True), f("ownerName", "Owner Name"), f("spvEntity", "SPV/Entity"), f("titleStatus", "Title Status"), f("encumbranceStatus", "Encumbrance Status"), f("legalDisputes", "Legal Disputes"), f("approvals", "Approvals"), f("leaseOwnershipDocuments", "Lease/Ownership Documents")], master_label="Ownership & Legal Master"),
    "physical_details": section("physical_details", "Physical Details", "assetId", [f("assetId", "Asset ID", "system", required=True), f("landArea", "Land Area (sq ft)", data_type="number"), f("builtUpArea", "Built-up Area (sq ft)", data_type="number"), f("carpetArea", "Carpet Area (sq ft)", data_type="number"), f("leasableArea", "Leasable Area (sq ft)", data_type="number"), f("floors", "No. of Floors", data_type="integer"), f("units", "No. of Units", data_type="integer"), f("ageOfProperty", "Age of Property (yrs)", data_type="number"), f("conditionScore", "Condition Score", data_type="number")], master_label="Physical Details Master"),
    "esg_data": section("esg_data", "ESG Data", "assetId", [f("assetId", "Asset ID", "system", required=True), f("energyConsumption", "Energy Consumption (kWh/yr)", data_type="number"), f("waterConsumption", "Water Consumption (KL/yr)", data_type="number"), f("emissions", "Emissions (tCO2e/yr)", "derived", "number"), f("wasteGenerated", "Waste Generated (kg/yr)", data_type="number"), f("greenCertification", "Green Certification"), f("climateRisk", "Climate Risk"), f("complianceStatus", "Compliance Status", "derived"), f("tenantHealthSafetyScore", "Tenant Health/Safety Score", data_type="number"), f("esgRiskScore", "ESG Risk Score", "derived", "number")], master_label="ESG Data Master"),
    "financial_value": section("financial_value", "Financial Value", "assetId", [f("assetId", "Asset ID", "system", required=True), f("acquisitionCost", "Acquisition Cost (₹)", data_type="currency"), f("currentMarketValue", "Current Market Value (₹)", data_type="currency"), f("bookValue", "Book Value (₹)", data_type="currency"), f("valuationDate", "Valuation Date", data_type="date"), f("valuationMethod", "Valuation Method"), f("appreciationDepreciation", "Appreciation/Depreciation (%)", "derived", "percentage")], master_label="Financial Value Master"),
    "revenue_income": section("revenue_income", "Revenue & Income", "assetId", [f("assetId", "Asset ID", "system", required=True), f("rentalIncome", "Rental Income (₹/yr)", data_type="currency"), f("occupancyIncome", "Occupancy Income (₹/yr)", "derived", "currency"), f("otherIncome", "Other Income (₹/yr)", data_type="currency"), f("totalIncome", "Total Income (₹/yr)", "derived", "currency"), f("escalationClause", "Escalation Clause (%)", data_type="percentage"), f("collectionEfficiency", "Collection Efficiency (%)", data_type="percentage")], master_label="Revenue & Income Master"),
    "expenses": section("expenses", "Expenses", "assetId", [f("assetId", "Asset ID", "system", required=True), f("maintenanceCost", "Maintenance Cost (₹/yr)", data_type="currency"), f("propertyTax", "Property Tax (₹/yr)", data_type="currency"), f("insurance", "Insurance (₹/yr)", data_type="currency"), f("utilities", "Utilities (₹/yr)", data_type="currency"), f("repairs", "Repairs (₹/yr)", data_type="currency"), f("capex", "Capex (₹/yr)", data_type="currency"), f("opex", "Opex (₹/yr)", "derived", "currency")], master_label="Expenses Master"),
    "occupancy_leasing": section("occupancy_leasing", "Occupancy / Leasing", "assetId", [f("assetId", "Asset ID", "system", required=True), f("occupancy", "Occupancy (%)", data_type="percentage"), f("vacantArea", "Vacant Area (sq ft)", "derived", "number"), f("tenantName", "Tenant Name"), f("leaseStartDate", "Lease Start Date", data_type="date"), f("leaseEndDate", "Lease End Date", data_type="date"), f("lockInPeriod", "Lock-in Period (months)", data_type="integer"), f("renewalStatus", "Renewal Status")], master_label="Occupancy / Leasing Master"),
    "loan_debt": section("loan_debt", "Loan / Debt", "assetId", [f("assetId", "Asset ID", "system", required=True), f("loanAmount", "Loan Amount (₹)", data_type="currency"), f("lender", "Lender"), f("interestRate", "Interest Rate (%)", data_type="percentage"), f("loanTenure", "Loan Tenure (months)", data_type="integer"), f("emi", "EMI (₹/month)", "derived", "currency"), f("outstandingPrincipal", "Outstanding Principal (₹)", data_type="currency"), f("ltv", "LTV (%)", "derived", "percentage"), f("dscr", "DSCR", "derived", "number"), f("repaymentSchedule", "Repayment Schedule")], master_label="Loan / Debt Master"),
    "risk_fields": section("risk_fields", "Risk Fields", "assetId", [f("assetId", "Asset ID", "system", required=True), f("legalRisk", "Legal Risk"), f("marketRisk", "Market Risk"), f("tenantRisk", "Tenant Risk"), f("valuationRisk", "Valuation Risk"), f("liquidityRisk", "Liquidity Risk"), f("regulatoryRisk", "Regulatory Risk"), f("redFlagScore", "Red Flag Score", "derived", "number")], master_label="Risk Fields Master"),
    "performance_metrics": section("performance_metrics", "Performance Metrics", "assetId", [f("assetId", "Asset ID", "system", required=True), f("noi", "NOI (₹/yr)", "derived", "currency"), f("ebitda", "EBITDA (₹/yr)", "derived", "currency"), f("capRate", "Cap Rate (%)", "derived", "percentage"), f("irr", "IRR (%)", data_type="percentage"), f("roi", "ROI (%)", "derived", "percentage"), f("yieldPercentage", "Yield (%)", "derived", "percentage"), f("paybackPeriod", "Payback Period (yrs)", "derived", "number"), f("cashOnCashReturn", "Cash-on-Cash Return (%)", "derived", "percentage")], master_label="Performance Metrics Master"),
    "cash_flow_analysis": section("cash_flow_analysis", "Cash Flow Analysis", "assetId", [f("assetId", "Asset ID", "system", required=True), f("cashFlowPeriod", "Cash Flow Period"), f("openingValue", "Opening Value (₹)", data_type="currency"), f("grossRentalIncome", "Gross Rental Income (₹/yr)", "derived", "currency"), f("otherIncome", "Other Income (₹/yr)", data_type="currency"), f("totalGrossIncome", "Total Gross Income (₹/yr)", "derived", "currency"), f("operatingExpenses", "Operating Expenses (₹/yr)", "derived", "currency"), f("noi", "NOI (₹/yr)", "derived", "currency"), f("annualDebtService", "Annual Debt Service (₹/yr)", "derived", "currency"), f("netCashFlowAfterDebt", "Net Cash Flow After Debt (₹/yr)", "derived", "currency"), f("capexReserve", "Capex Reserve (₹/yr)", data_type="currency"), f("freeCashFlow", "Free Cash Flow (₹/yr)", "derived", "currency"), f("dscr", "DSCR", "derived", "number"), f("cashYield", "Cash Yield (%)", "derived", "percentage"), f("remarks", "Remarks")], master_label="Cash Flow Analysis Master"),
    "market_benchmarking": section("market_benchmarking", "Market Benchmarking", "assetId", [f("assetId", "Asset ID", "system", required=True), f("marketRent", "Market Rent (₹/sq ft/month)", data_type="currency"), f("marketRate", "Market Rate (₹/sq ft)", data_type="currency"), f("comparableSales", "Comparable Sales"), f("vacancyRate", "Vacancy Rate (%)", data_type="percentage"), f("absorption", "Absorption (units/month)", data_type="number"), f("competitorSupply", "Competitor Supply (units)", data_type="number")], master_label="Market Benchmarking Master"),
    "documents": section("documents", "Documents", "assetId", [f("assetId", "Asset ID", "system", required=True), f("titleDeed", "Title Deed", data_type="file"), f("valuationReport", "Valuation Report", data_type="file"), f("leaseAgreement", "Lease Agreement", data_type="file"), f("taxReceipt", "Tax Receipt", data_type="file"), f("insurancePolicy", "Insurance Policy", data_type="file"), f("approvalDocuments", "Approval Documents", data_type="file")], master_label="Documents Master"),
    "workflow_actions": section("workflow_actions", "Workflow / Actions", "assetId", [f("assetId", "Asset ID", "system", required=True), f("assignedUser", "Assigned User"), f("taskStatus", "Task Status"), f("nextReviewDate", "Next Review Date", data_type="date"), f("approvalStatus", "Approval Status"), f("remarks", "Remarks"), f("alerts", "Alerts", "derived"), f("auditTrail", "Audit Trail")], master_label="Workflow / Actions Master"),
    "exit_strategy": section("exit_strategy", "Exit / Strategy", "assetId", [f("assetId", "Asset ID", "system", required=True), f("acquisitionDate", "Acquisition Date", data_type="date"), f("acquisitionCost", "Acquisition Cost (₹)", data_type="currency"), f("holdSellRedevelopDecision", "Hold/Sell/Redevelop Decision"), f("targetExitValue", "Target Exit Value (₹)", data_type="currency"), f("expectedSaleDate", "Expected Sale Date", data_type="date"), f("exitIrr", "Exit IRR (%)", "derived", "percentage"), f("buyerInterest", "Buyer Interest")], master_label="Exit / Strategy Master"),
    "operations_maintenance": section("operations_maintenance", "Operations & Maintenance", "assetId", [f("assetId", "Asset ID", "system", required=True), f("repairsStatus", "Repairs Status"), f("amcStatus", "AMC Status"), f("inspectionStatus", "Inspection Status"), f("complaintsOpen", "Complaints Open", data_type="integer"), f("utilitiesStatus", "Utilities Status"), f("vendorName", "Vendor Name")], master_label="Operations & Maintenance Master"),
}


def _with_display_metadata(item, index):
    section_item = deepcopy(item)
    section_item["display_order"] = index
    section_item["display_label"] = f"{index}. {section_item['label']}"
    return section_item


def list_sections():
    return [_with_display_metadata(item, index) for index, item in enumerate(SECTIONS.values(), start=1)]


def get_section(section_key: str):
    if section_key not in SECTIONS:
        raise KeyError(section_key)
    index = list(SECTIONS).index(section_key) + 1
    return _with_display_metadata(SECTIONS[section_key], index)


def upload_sections():
    return [item for item in list_sections() if item["upload_enabled"]]
