

PROJECT_QUERY_SCHEMA = """
Use only the following tables, columns, and meanings.

projects:
  project_id: Unique project identifier
  internal_index_id: Internal system project ID
  registered_project_name: Project name as registered
  project_name: Cleaned or standard project name
  location_id: Location identifier
  location_name: Location name
  city_name: City name
  project_latitude: Project latitude
  project_longitude: Project longitude
  city_id: City identifier
  location_latitude: Location latitude
  location_longitude: Location longitude
  plot_number: Plot number
  project_registration_id: RERA or project registration ID
  is_coordinate_manually_done: Manual coordinate flag
  total_units: Total units in project
  booked_units: Units sold in project
  commencement_date: Project start date
  building_wise_total_booked_units: Total and sold units by building
  final_proposed_date_of_completion: Expected completion date
  project_bhk_summary: BHK-wise project summary
  project_commencement_quarter_units: Commenced quarter and total units per project
  organization_individual_name: Developer or promoter name
  number_of_developers: Total developers count
  pincode: Project postal code
  registered_project_count: Total project count
  remark: Additional comments
  total_fsi: Total proposed FSI
  total_plot_area_sq_m: Plot area in square meters
  bhk_wise_min_max_area: Min/max area by BHK
  bhk_wise_carpet_area: Carpet area by BHK
  project_type: Project type such as residential or commercial
  bhk_wise_total_booked_units: Total and sold units by BHK
  carpet_wise_total_booked_units: Total and sold units by area
  total_building_count: Number of buildings
  project_tower_completion_date: Tower completion date
  number_of_sanctioned_floors: Approved floor count
  amenity_profile: Amenities list or score
  age_of_project: Project age
  construction_status: Construction stage
  building_grade: Building quality grade
  zoning_type: Zoning classification
  encumbrance_status: Legal encumbrance status
  country_name: Country name
  state_name: State name
  sub_locality: Sub-local area
  micro_market: Micro market area
  frontage: Road frontage detail
  approval_status: Approval status
  data_source: Source of data such as RERA or DLD
  source_accessibility: Data access status
  source_accessibility_way: Access method such as api, download, or mining
  sourcing_cost: Processing or source cost
  sourcing_time: Processing time
  rera_location_v1: RERA location text
  data_type: Registered project

Semantic project category columns:
  The following columns often contain repeated categorical values and should be
  semantically resolved against distinct database values before SQL generation:
  project_type
"""
