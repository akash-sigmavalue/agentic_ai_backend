# Architecture

The portfolio backend is integrated under namespaced folders so it can live beside the existing agents, connector, valuation, geospatial, web-search, and UI-creation modules.

- `main.py` owns app creation, middleware, startup, and portfolio router registration.
- `api/routes/portfolio` owns portfolio HTTP endpoints.
- `api/schemas/portfolio` owns portfolio request schemas.
- `core` owns shared settings and LLM construction.
- `database/portfolio` owns portfolio SQLAlchemy models, sessions, repositories, and schema setup.
- `registry/portfolio` owns the frontend section/field contract.
- `services/portfolio` owns upload orchestration, records, derived calculations, and dashboard aggregation.
- `tools/portfolio` owns Excel/CSV detection, profiling, validation, and helpers.
- `agents/mapping_agent` owns the AI semantic mapping agent used by portfolio uploads.

## Mapping Agent Scope

The mapping agent is dynamic, not statically hardcoded.

- Section upload: `services/portfolio/upload_service.py` passes only the selected section from `registry.portfolio.registry` into `MappingAgent.map_section_columns()`.
- Global upload: `services/portfolio/upload_service.py` passes all upload-enabled frontend sections into `MappingAgent.map_global_table()` for each detected table. The agent can return mappings for multiple sections from one table when the uploaded columns contain mixed portfolio data.

The agent uses the shared `core.llm.get_llm()` provider, so it follows this repo's configured OpenAI model. The system prompt lives in `agents/mapping_agent/prompts.py`.
