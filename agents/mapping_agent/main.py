import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from agents.mapping_agent.prompts import SECTION_MAPPING_REPAIR_PROMPT, SECTION_MAPPING_REVIEW_PROMPT, SECTION_MAPPING_SYSTEM_PROMPT
from agents.mapping_agent.schemas import MappingAgentResult, MultiSectionMappingAgentResult, SectionMappingResult
from agents.mapping_agent.tools import build_section_payload
from core.config import settings
from core.llm import get_llm


logger = logging.getLogger(__name__)


class MappingAgent:
    name = "mapping_agent"
    description = "Maps uploaded table columns to frontend section fields using scoped AI semantics only."

    def map_section_columns(self, section: dict, column_profile: list[dict], sample_rows: list[dict] | None = None, table_context: dict | None = None) -> dict:
        payload = {
            "upload_mode": "section",
            "section_key": section["section_key"],
            "sections": [build_section_payload(section)],
            "column_profile": column_profile,
            "sample_rows": sample_rows or [],
            "table_context": table_context or {},
        }
        return self._invoke(payload, [section])

    def map_global_table(self, sections: list[dict], column_profile: list[dict], sample_rows: list[dict] | None = None, table_context: dict | None = None) -> dict:
        payload = {
            "upload_mode": "global",
            "instruction": "Inspect all frontend sections and return every section whose fields can be filled from this uploaded table.",
            "sections": [build_section_payload(section) for section in sections],
            "column_profile": column_profile,
            "sample_rows": sample_rows or [],
            "table_context": table_context or {},
        }
        return self._invoke(payload, sections)

    # Backward-compatible alias used by older service code.
    def map_columns(self, section: dict, column_profile: list[dict]) -> dict:
        return self.map_section_columns(section, column_profile)

    def _invoke(self, payload: dict, sections: list[dict]) -> dict:
        self._debug("payload sent to mapping agent", payload)
        llm = get_llm(temperature=0)
        if llm is None:
            result = self._empty_result(
                payload,
                "AI mapping is unavailable because OPENAI_API_KEY is not configured.",
            )
            self._debug("final mapping agent output", result)
            return result
        try:
            draft = self._call_json(
                llm,
                SECTION_MAPPING_SYSTEM_PROMPT,
                payload,
                "raw ai draft mapping response",
            )
            reviewed = self._review_mapping(llm, payload, draft)
            sanitized = self._sanitize(reviewed, payload, sections)
            sanitized = self._repair_until_acceptable(llm, payload, sections, sanitized)
            sanitized = self._with_coverage_warnings(sanitized, payload)
            self._debug("final mapping agent output", sanitized)
            self._print_mapping_summary(sanitized, payload)
            return sanitized
        except Exception as exc:
            logger.exception("AI mapping failed")
            result = self._empty_result(payload, f"AI mapping failed: {exc}")
            self._debug("final mapping agent output", result)
            return result

    def _call_json(self, llm, system_prompt: str, payload: dict, debug_label: str) -> dict:
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=json.dumps(payload, default=str)),
        ])
        content = response.content if hasattr(response, "content") else str(response)
        self._debug(debug_label, content)
        return self._parse_json(content)

    def _review_mapping(self, llm, payload: dict, draft: dict) -> dict:
        if not settings.MAPPING_AGENT_REVIEW_ENABLED:
            return draft
        try:
            review_payload = {
                "original_payload": payload,
                "first_pass_mapping": draft,
                "review_instruction": "Return the corrected final mapping JSON only.",
            }
            return self._call_json(
                llm,
                SECTION_MAPPING_REVIEW_PROMPT,
                review_payload,
                "raw ai reviewed mapping response",
            )
        except Exception as exc:
            self._debug("ai review failed; using first-pass mapping", {"error": str(exc), "first_pass_mapping": draft})
            return draft

    def _repair_until_acceptable(self, llm, payload: dict, sections: list[dict], sanitized: dict) -> dict:
        if not settings.MAPPING_AGENT_REVIEW_ENABLED:
            return sanitized

        current = sanitized
        max_rounds = max(int(settings.MAPPING_AGENT_REPAIR_ROUNDS or 0), 0)
        for round_number in range(max_rounds):
            coverage = self._coverage_report(current, payload)
            if round_number > 0 and coverage["acceptable"]:
                return current
            try:
                repair_payload = {
                    "original_payload": payload,
                    "current_mapping": current,
                    "coverage_report": coverage,
                    "repair_round": round_number + 1,
                }
                repaired = self._call_json(
                    llm,
                    SECTION_MAPPING_REPAIR_PROMPT,
                    repair_payload,
                    f"raw ai repair mapping response round {round_number + 1}",
                )
                current = self._sanitize(repaired, payload, sections)
            except Exception as exc:
                self._debug("ai repair failed; keeping current mapping", {"error": str(exc), "coverage_report": coverage})
                return current
        return current

    def _parse_json(self, content: str) -> dict:
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                raise
            return json.loads(match.group(0))

    def _sanitize(self, result: dict, payload: dict, sections: list[dict]) -> dict:
        if payload.get("upload_mode") == "global":
            return self._sanitize_global(result, payload, sections)
        return self._sanitize_section(result, payload, sections[0])

    def _sanitize_section(self, result: dict, payload: dict, section: dict) -> dict:
        section_key = payload["section_key"]
        section_result = self._sanitize_one_section(result, payload, section, require_all_unmapped=True)
        model = MappingAgentResult(
            section_key=section_key,
            section_confidence=section_result.section_confidence,
            mappings=section_result.mappings,
            unmapped_columns=section_result.unmapped_columns,
            warnings=section_result.warnings,
        )
        return model.model_dump()

    def _sanitize_global(self, result: dict, payload: dict, sections: list[dict]) -> dict:
        section_map = {section["section_key"]: section for section in sections}
        raw_sections = result.get("section_mappings") or []
        if not raw_sections and result.get("section_key"):
            raw_sections = [result]

        section_results = []
        used_columns = set()
        for raw in raw_sections:
            section_key = raw.get("section_key")
            section = section_map.get(section_key)
            if not section:
                continue
            sanitized = self._sanitize_one_section(raw, payload, section, require_all_unmapped=False)
            if not sanitized.mappings:
                continue
            used_columns.update(item.uploaded_column for item in sanitized.mappings)
            section_results.append(sanitized)

        uploaded = {item["column_name"] for item in payload.get("column_profile", [])}
        raw_unmapped = result.get("unmapped_columns") or []
        unmapped = []
        seen_unmapped = set()
        for item in raw_unmapped:
            uploaded_column = item.get("uploaded_column")
            if uploaded_column in uploaded and uploaded_column not in used_columns and uploaded_column not in seen_unmapped:
                seen_unmapped.add(uploaded_column)
                unmapped.append({
                    "uploaded_column": uploaded_column,
                    "target_field": None,
                    "confidence": 0,
                    "status": "custom_field",
                    "reason": item.get("reason", "No relevant field in any allowed section."),
                })
        for column in uploaded - used_columns - seen_unmapped:
            unmapped.append({
                "uploaded_column": column,
                "target_field": None,
                "confidence": 0,
                "status": "custom_field",
                "reason": "No relevant field in any allowed section.",
            })

        primary = max(section_results, key=lambda item: item.section_confidence, default=None)
        model = MultiSectionMappingAgentResult(
            section_key=primary.section_key if primary else None,
            section_confidence=primary.section_confidence if primary else 0,
            mappings=primary.mappings if primary else [],
            unmapped_columns=unmapped,
            warnings=result.get("warnings", []),
            section_mappings=section_results,
        )
        return model.model_dump()

    def _sanitize_one_section(self, result: dict, payload: dict, section: dict, *, require_all_unmapped: bool) -> SectionMappingResult:
        uploaded = {item["column_name"] for item in payload.get("column_profile", [])}
        allowed = {field["key"] for field in section["fields"]}
        used_columns = set()
        mappings = []
        unmapped = []

        for item in result.get("mappings", []):
            uploaded_column = item.get("uploaded_column")
            target_field = item.get("target_field")
            if uploaded_column not in uploaded:
                continue
            used_columns.add(uploaded_column)
            if target_field not in allowed:
                unmapped.append({
                    "uploaded_column": uploaded_column,
                    "target_field": None,
                    "confidence": 0,
                    "status": "custom_field",
                    "reason": "Agent suggested a field outside the scoped section.",
                })
                continue
            if not self._is_type_compatible(uploaded_column, target_field, section, payload):
                unmapped.append({
                    "uploaded_column": uploaded_column,
                    "target_field": None,
                    "confidence": 0,
                    "status": "custom_field",
                    "reason": "Mapped field rejected because uploaded values do not match the target field data type.",
                })
                continue
            confidence = max(0.0, min(float(item.get("confidence") or 0), 1.0))
            mappings.append({
                "uploaded_column": uploaded_column,
                "target_field": target_field,
                "confidence": confidence,
                "status": item.get("status") or ("auto_mapped" if confidence >= 0.85 else "needs_review"),
                "reason": item.get("reason", ""),
            })

        for item in result.get("unmapped_columns", []):
            uploaded_column = item.get("uploaded_column")
            if uploaded_column in uploaded and uploaded_column not in used_columns:
                used_columns.add(uploaded_column)
                unmapped.append({
                    "uploaded_column": uploaded_column,
                    "target_field": None,
                    "confidence": 0,
                    "status": "custom_field",
                    "reason": item.get("reason", "No mapping selected."),
                })
        if require_all_unmapped:
            for column in uploaded - used_columns:
                unmapped.append({
                    "uploaded_column": column,
                    "target_field": None,
                    "confidence": 0,
                    "status": "custom_field",
                    "reason": "No mapping selected.",
                })

        return SectionMappingResult(
            section_key=section["section_key"],
            section_confidence=max(0.0, min(float(result.get("section_confidence") or 0), 1.0)),
            mappings=mappings,
            unmapped_columns=unmapped,
            warnings=result.get("warnings", []),
        )

    def _profile_by_column(self, payload: dict) -> dict:
        return {item["column_name"]: item for item in payload.get("column_profile", [])}

    def _field_by_key(self, section: dict) -> dict:
        return {field["key"]: field for field in section.get("fields", [])}

    def _is_type_compatible(self, uploaded_column: str, target_field: str, section: dict, payload: dict) -> bool:
        profile = self._profile_by_column(payload).get(uploaded_column, {})
        field = self._field_by_key(section).get(target_field, {})
        data_type = field.get("data_type")
        detected_type = profile.get("detected_type")
        date_ratio = float(profile.get("date_ratio") or 0)
        numeric_ratio = float(profile.get("numeric_ratio") or 0)
        looks_like_date = bool(profile.get("looks_like_date"))

        if data_type == "integer" and (looks_like_date or date_ratio >= 0.5 or detected_type == "date"):
            return False
        if data_type == "date" and detected_type not in {None, "date", "text", "number"}:
            return False
        if data_type in {"currency", "number", "percentage", "integer"} and detected_type == "text" and numeric_ratio == 0 and not profile.get("looks_like_amount") and not profile.get("looks_like_percentage"):
            return False
        return True

    def _with_coverage_warnings(self, result: dict, payload: dict) -> dict:
        coverage = self._coverage_report(result, payload)
        if not coverage["uploaded_columns"]:
            return result
        warnings = list(result.get("warnings", []))

        if coverage["missing_decision_columns"]:
            warnings.append(f"Mapping coverage warning: {len(coverage['missing_decision_columns'])} uploaded columns were not mapped or explicitly marked unmapped: {', '.join(coverage['missing_decision_columns'][:10])}.")
        if coverage["low_coverage"]:
            warnings.append(f"Low mapping coverage: only {coverage['mapped_count']} of {coverage['uploaded_count']} uploaded columns were mapped. Review section selection and AI response.")
        for report in coverage["section_gap_reports"]:
            unmapped_columns = [item["column_name"] for item in report.get("unmapped_column_profiles", []) if item.get("column_name")]
            unused_fields = [field["key"] for field in report.get("unused_fields", []) if field.get("key")]
            warnings.append(
                f"Mapping completeness gap in {report['section_key']}: unmapped columns {', '.join(unmapped_columns[:10])} should be compared with unused fields {', '.join(unused_fields[:10])}."
            )

        result["warnings"] = warnings
        return result

    def _coverage_report(self, result: dict, payload: dict) -> dict:
        uploaded = {item["column_name"] for item in payload.get("column_profile", [])}
        profiles = self._profile_by_column(payload)
        if payload.get("upload_mode") == "global":
            mapped = {
                item.get("uploaded_column")
                for section in result.get("section_mappings", [])
                for item in section.get("mappings", [])
                if item.get("uploaded_column")
            }
        else:
            mapped = {item.get("uploaded_column") for item in result.get("mappings", []) if item.get("uploaded_column")}

        unmapped = {item.get("uploaded_column") for item in result.get("unmapped_columns", []) if item.get("uploaded_column")}
        missing_decision = sorted(uploaded - mapped - unmapped)
        mapped_ratio = len(mapped) / max(len(uploaded), 1)
        low_coverage = len(uploaded) >= 5 and mapped_ratio < 0.5
        gap_reports = self._section_gap_reports(result, payload)
        needs_gap_repair = bool(gap_reports)
        return {
            "acceptable": not missing_decision and not low_coverage and not needs_gap_repair,
            "uploaded_columns": sorted(uploaded),
            "uploaded_count": len(uploaded),
            "mapped_columns": sorted(mapped),
            "mapped_count": len(mapped),
            "unmapped_columns": sorted(unmapped),
            "unmapped_column_profiles": [profiles[column] for column in sorted(unmapped) if column in profiles],
            "unmapped_count": len(unmapped),
            "missing_decision_columns": missing_decision,
            "mapped_ratio": round(mapped_ratio, 3),
            "low_coverage": low_coverage,
            "section_gap_reports": gap_reports,
        }

    def _section_gap_reports(self, result: dict, payload: dict) -> list[dict]:
        profiles = self._profile_by_column(payload)
        payload_sections = {section["section_key"]: section for section in payload.get("sections", [])}

        if payload.get("upload_mode") == "global":
            section_results = result.get("section_mappings", [])
            global_unmapped = {
                item.get("uploaded_column")
                for item in result.get("unmapped_columns", [])
                if item.get("uploaded_column")
            }
        else:
            section_results = [result] if result.get("section_key") else []
            global_unmapped = {
                item.get("uploaded_column")
                for item in result.get("unmapped_columns", [])
                if item.get("uploaded_column")
            }

        reports = []
        for section_result in section_results:
            section_key = section_result.get("section_key")
            section = payload_sections.get(section_key)
            if not section:
                continue

            mapped_fields = {
                item.get("target_field")
                for item in section_result.get("mappings", [])
                if item.get("target_field")
            }
            mapped_columns = {
                item.get("uploaded_column")
                for item in section_result.get("mappings", [])
                if item.get("uploaded_column")
            }
            section_unmapped = {
                item.get("uploaded_column")
                for item in section_result.get("unmapped_columns", [])
                if item.get("uploaded_column")
            }
            candidate_unmapped = sorted((global_unmapped | section_unmapped) - mapped_columns)
            unused_fields = [
                field
                for field in section.get("fields", [])
                if field.get("key") not in mapped_fields
            ]

            mapped_count = len(mapped_columns)
            section_confidence = float(section_result.get("section_confidence") or 0)
            looks_like_clean_section = section_confidence >= 0.7 and mapped_count >= 3
            if not looks_like_clean_section or not candidate_unmapped or not unused_fields:
                continue

            reports.append({
                "section_key": section_key,
                "section_label": section.get("label"),
                "section_confidence": section_confidence,
                "message": "This section is mostly mapped but still has unmapped uploaded columns and unused allowed fields. Recompare each unmapped column with each unused field before leaving it custom.",
                "unmapped_column_profiles": [
                    profiles[column]
                    for column in candidate_unmapped
                    if column in profiles
                ],
                "unused_fields": unused_fields,
            })

        return reports

    def _empty_result(self, payload: dict, reason: str) -> dict:
        uploaded = [item["column_name"] for item in payload.get("column_profile", [])]
        unmapped = [
            {
                "uploaded_column": column,
                "target_field": None,
                "confidence": 0,
                "status": "custom_field",
                "reason": reason,
            }
            for column in uploaded
        ]
        if payload.get("upload_mode") == "global":
            return MultiSectionMappingAgentResult(
                section_key=None,
                section_confidence=0,
                mappings=[],
                unmapped_columns=unmapped,
                section_mappings=[],
                warnings=[reason],
            ).model_dump()
        return MappingAgentResult(
            section_key=payload.get("section_key"),
            section_confidence=0,
            mappings=[],
            unmapped_columns=unmapped,
            warnings=[reason],
        ).model_dump()

    def _print_mapping_summary(self, result: dict, payload: dict) -> None:
        if not settings.DEBUG_MAPPING_AGENT:
            return

        lines = [
            "MAPPING_AGENT_RESULT",
            f"mode: {payload.get('upload_mode')}",
            f"sheet: {(payload.get('table_context') or {}).get('sheet_name')}",
            f"table_index: {(payload.get('table_context') or {}).get('table_index')}",
        ]

        if payload.get("upload_mode") == "global":
            section_mappings = result.get("section_mappings", [])
        else:
            section_mappings = [result] if result.get("section_key") else []

        if not section_mappings:
            lines.append("mapped_sections: none")

        for section in section_mappings:
            lines.append(f"{section.get('section_key')}:")
            mappings = section.get("mappings", [])
            if not mappings:
                lines.append("  mapped_fields: none")
                continue
            for item in mappings:
                lines.append(f"  {item.get('uploaded_column')} -> {item.get('target_field')}")

        warnings = result.get("warnings") or []
        if warnings:
            lines.append("warnings:")
            for warning in warnings:
                lines.append(f"  {warning}")

        print("\n".join(lines), flush=True)

    def _debug(self, label: str, data) -> None:
        if not settings.DEBUG_MAPPING_AGENT:
            return
        try:
            rendered = json.dumps(data, indent=2, default=str)
        except TypeError:
            rendered = str(data)
        print(f"\nMAPPING_AGENT_DEBUG: {label}\n{rendered}", flush=True)
