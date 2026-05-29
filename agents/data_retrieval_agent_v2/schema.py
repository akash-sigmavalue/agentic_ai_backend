
TRANSACTION_QUERY_SCHEMA = """
Use only the following tables, columns, and meanings.

transactions:
  project_id: Unique project identifier
  internal_index_id: Internal system project ID
  project_name: Name of project
  village_name_marathi: Village name in Marathi
  location_id: Location identifier
  location_name: Location of transaction
  village_name: Village name in English
  year: Transaction year
  quarter: Transaction quarter
  city_id: City identifier
  city_name: City of transaction
  transaction_category_id: Transaction category ID
  sub_registrar_office_code: SRO office code
  sub_registrar_office_name: SRO office name
  document_number: Registration document number
  transaction_type: Type of transaction
  agreement_price: Deal/agreement/property value
  guideline_value: Government determined value
  property_description: Property details text
  transaction_date: Date of transaction
  floor_number: Floor of unit
  unit_number: Identifier for a specific apartment, flat, or suite within a building
  property_type_raw: Raw property type text
  net_carpet_area_sq_m: Net carpet area in square meters
  balcony_sq_m: Balcony area in square meters
  terrace_sq_m: Terrace area in square meters
  seller_name: Seller name
  buyer_name: Buyer name
  transaction_category: Category of transaction such as sale, lease, others
  internal_document_number: Internal doc reference
  micr_number: Bank MICR number
  bank_type: Bank type or category
  party_code: Party classification code
  date_of_agreement_execution: Agreement execution date
  stamp_duty_paid: Stamp duty amount
  registration_fee: Registration fee paid
  project_latitude: Project latitude
  project_longitude: Project longitude
  location_latitude: Location latitude
  location_longitude: Location longitude
  property_type: Standardized property type
  unit_configuration: BHK configuration
  buyer_pincode: Buyer postal code
  buyer_locality: Buyer locality
  buyer_district: Buyer district
  buyer_state: Buyer state
  is_llm_processed: Processed by AI flag
  is_manual_processed: Processed manually flag
  tower_name: Building or tower name
  is_duplicate: Duplicate record flag
  sale_type: Primary or secondary sale
  project_type: Residential or commercial type
  country_name: Country name
  state_name: State name
  micro_market: Micro market area
  sub_locality: Sub-local area
  pincode: Property postal code
  parking_count: Number of parking slots
  facing_direction: Property facing direction
  view_type: View from property
  furnishing_status: Furnishing level
  condition_status: Property condition
  source_accessibility: Data access status
  source_accessibility_way: Access method such as api or download
  sourcing_cost: Processing or source cost
  sourcing_time: Processing time
  data_type: Registered document
  data_source: Source of data such as IGR or DLD

Semantic category columns:
  The following columns often contain repeated categorical values and should be
  semantically resolved against distinct database values before SQL generation:
  transaction_category, property_type, unit_configuration, project_type,
  sale_type, furnishing_status, condition_status, facing_direction, view_type,
  bank_type.
"""


SPACE_SCHEMA="""
1 | unit | unit_number
2 | building | tower_name
3 | parcel/survey/CTS/khasra/plot no | plot_number
4 | project | project_name
5 | location | location_name
6 | micromarket | micro_market
7 | city | city_name
8 | state | state_name
9 | country | country_name
"""