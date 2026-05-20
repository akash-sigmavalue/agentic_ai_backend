from __future__ import annotations

import os
import json
import time
import math
import threading
import logging
import re
import pandas as pd
import numpy as np
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict, Tuple, Any
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logger = logging.getLogger("data_cleaning")

# Optional fast json repair
try:
    
    # pyrefly: ignore [missing-import]
    from json_repair import repair_json
    _HAS_JSON_REPAIR = True
except ImportError:
    _HAS_JSON_REPAIR = False

_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

# --- Prompts ---
def build_cleaning_system_prompt(subject_name: str, comps: List[str]) -> str:
    target_block = json.dumps([{"name": subject_name}] + [{"name": c} for c in comps], indent=2)
    return f"""You are an expert real-estate data cleaning agent.
YOUR TASK: Receive a JSON array of raw scraped property listings.
For EACH row:
1. Determine if it belongs to one of the target projects based on name/location.
2. Ensure price and area are numeric. Split ranges into two rows (min/max).
3. Return ONLY a JSON array of cleaned objects.

## TARGET PROJECTS
{target_block}

## OUTPUT FORMAT (STRICT JSON)
{{
  "data": [
    {{
      "match_project": "string — canonical name from target list, or null",
      "relevant_for_valuation": true | false,
      "irrelevance_reason": "string or null",
      "price_value": number | null,
      "area_sqft": number | null,
      "area_type": "carpet" | "built_up" | "super_built_up" | "unknown",
      "config": "string (e.g. '2 BHK')",
      "possession_status": "string",
      "listing_type": "Sale" | "Rental" | null,
      "floor": "string or null",
      "total_floors": "string or null",
      "currency": "string or null (e.g. '₹', 'INR')",
      "range_row": "min" | "max" | "single",
      "road_type": "string (A, B, C, or D)",
      "raw_row_id": number
    }}
  ]
}}

RULES:
- A listing matches if its project name/location strongly suggests it belongs to a target project.
- DO NOT invent data. If price/area cannot be determined, set to null.
- Extract numbers cleanly (e.g. "60 Lac" -> 6000000). We already tried parsing, if a numeric value is provided in the input, keep it unless you know it's wrong.
- RANGE EXPANSION: If price OR area is a range (e.g. "45 - 50 Lac"), output TWO objects: one with min values (range_row="min"), one with max values (range_row="max").
- Keep the `raw_row_id` exactly as passed in so we can merge back.
"""

def extract_json_array(text: str) -> Optional[str]:
    start = text.find("{")
    if start == -1: return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: return text[start: i + 1]
    return None

def _parse_price_str(s: str) -> Optional[float]:
    if not s: return None
    s = str(s).strip().lower()
    s = re.sub(r"[â¹₹$£€,]", "", s).strip()
    try:
        m = re.search(r"[\d.]+", s)
        if not m: return None
        num = float(m.group())
        if "cr" in s or "crore" in s: return num * 1_00_00_000
        if "lac" in s or "lakh" in s: return num * 1_00_000
        if "k" in s: return num * 1_000
        if "m" in s and "month" not in s and "sqm" not in s: return num * 1_000_000
        return num
    except:
        return None

def _parse_area_str(s: str) -> Optional[float]:
    if not s: return None
    s = str(s).strip().lower()
    s = s.replace(",", "")
    m = re.search(r"([\d.]+)", s)
    if not m: return None
    val = float(m.group(1))
    if "sq.m" in s or "sqm" in s or "m²" in s or "sq m" in s: val = round(val * 10.7639, 2)
    elif "sq.yd" in s or "sqyd" in s: val = round(val * 9.0, 2)
    elif "acre" in s: val = round(val * 43560, 2)
    return round(val, 2)

def _is_range(s: str) -> bool:
    if not s: return False
    return bool(re.search(r"[\d]\s*[-–]\s*[\d]", str(s)))

def pre_process_normalisation(listings: List[Dict]) -> List[Dict]:
    """Applies fast Python parsing before LLM."""
    for row in listings:
        price_raw = str(row.get("price_raw") or "")
        area_raw = str(row.get("area_raw") or "")
        if not _is_range(price_raw):
             if not row.get("price"):
                row["price_parsed_py"] = _parse_price_str(price_raw)
        if not _is_range(area_raw):
             if not row.get("area_sqft"):
                row["area_parsed_py"] = _parse_area_str(area_raw)
    return listings

def deduplicate_listings(listings: List[Dict]) -> Tuple[List[Dict], int]:
    """Smart deduplication keeping the richest row."""
    seen = {}
    duplicates = 0
    
    for row in listings:
        proj = str(row.get("project_name", "")).strip().lower()
        bhk = str(row.get("bhk", "")).strip().lower()
        price = str(row.get("price", row.get("price_parsed_py", ""))).strip()
        area = str(row.get("area_sqft", row.get("area_parsed_py", ""))).strip()
        
        key = f"{proj}_{bhk}_{price}_{area}"
        
        # Count non-null fields to determine "richness"
        score = sum(1 for v in row.values() if v is not None and str(v).strip() != "")
        
        if key in seen:
            duplicates += 1
            if score > seen[key]["_score"]:
                row["_score"] = score
                row["is_duplicate"] = False
                seen[key]["is_duplicate"] = True
                seen[key] = row
            else:
                row["is_duplicate"] = True
        else:
            row["_score"] = score
            row["is_duplicate"] = False
            seen[key] = row
            
    return listings, duplicates

def process_batch(batch: List[Dict], sys_prompt: str, metrics: Dict) -> List[Dict]:
    user_content = json.dumps(batch, ensure_ascii=False)
    
    for attempt in range(3):
        try:
            response = _client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_content}
                ]
            )
            
            with threading.Lock():
                metrics["prompt_tokens"] += response.usage.prompt_tokens
                metrics["completion_tokens"] += response.usage.completion_tokens
                metrics["total_tokens"] += response.usage.total_tokens
                
            raw_text = response.choices[0].message.content
            
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                if _HAS_JSON_REPAIR:
                    parsed = json.loads(repair_json(raw_text))
                else:
                    raise ValueError("JSON decode failed")
                    
            return parsed.get("data", [])
        except Exception as e:
            logger.warning(f"Batch LLM error attempt {attempt}: {e}")
            time.sleep(2)
            
    # Fallback: empty for this batch
    logger.error("Batch failed completely.")
    return []

def apply_area_conversion(df: pd.DataFrame, is_apartment: bool) -> pd.DataFrame:
    """Computes and applies the data-driven carpet to super-builtup conversion factor."""
    if df.empty or not is_apartment:
        df["final_super_builtup_area"] = df["cleaned_area_sqft"]
        df["area_type_converted"] = False
        df["conversion_factor_used"] = 1.0
        return df

    # Find project-level median rates
    factors = {}
    builtup_factors = {}
    for proj in df["cleaned_match_project"].unique():
        if pd.isna(proj): continue
        proj_df = df[(df["cleaned_match_project"] == proj) & (df["cleaned_relevant_for_valuation"] == True)]
        
        carpet_df = proj_df[proj_df["cleaned_area_type"] == "carpet"]
        sbua_df = proj_df[proj_df["cleaned_area_type"] == "super_built_up"]
        builtup_df = proj_df[proj_df["cleaned_area_type"].isin(["built_up", "builtup"])]
        
        median_sbua_rate = None
        if len(sbua_df) >= 3:
            median_sbua_rate = (sbua_df["cleaned_price_value"] / sbua_df["cleaned_area_sqft"]).median()
            
        if median_sbua_rate and pd.notna(median_sbua_rate) and median_sbua_rate > 0:
            # 1. Carpet to SBUA Factor
            if len(carpet_df) >= 3:
                median_carpet_rate = (carpet_df["cleaned_price_value"] / carpet_df["cleaned_area_sqft"]).median()
                if pd.notna(median_carpet_rate):
                    c_factor = median_carpet_rate / median_sbua_rate
                    if 1.1 <= c_factor <= 1.5:
                        factors[proj] = round(c_factor, 3)
            
            # 2. Built-up to SBUA Factor
            if len(builtup_df) >= 3:
                median_builtup_rate = (builtup_df["cleaned_price_value"] / builtup_df["cleaned_area_sqft"]).median()
                if pd.notna(median_builtup_rate):
                    b_factor = median_builtup_rate / median_sbua_rate
                    if 1.05 <= b_factor <= 1.3:
                        builtup_factors[proj] = round(b_factor, 3)
                    
    df["final_super_builtup_area"] = df["cleaned_area_sqft"].astype(float)
    df["area_type_converted"] = False
    df["conversion_factor_used"] = 1.0
    
    for idx, row in df.iterrows():
        area_type = row.get("cleaned_area_type", "unknown")
        
        if area_type in ["carpet", "unknown", "built_up", "builtup"]:
            proj = row["cleaned_match_project"]
            base_factor = factors.get(proj, 1.25) # Default 1.25 fallback for carpet
            
            # If it's already built-up, it only needs a smaller jump to reach SBUA
            if area_type in ["built_up", "builtup"]:
                factor = builtup_factors.get(proj, 1.10) # Data-driven or 1.10 fallback
            else:
                factor = base_factor
                
            if pd.notna(row["cleaned_area_sqft"]):
                df.at[idx, "final_super_builtup_area"] = round(float(row["cleaned_area_sqft"]) * factor, 2)
                df.at[idx, "area_type_converted"] = True
                df.at[idx, "conversion_factor_used"] = factor
                
    return df

def stat_prescreening(df: pd.DataFrame) -> pd.DataFrame:
    """Flags extreme outliers using IQR method on rate per sqft."""
    if df.empty: return df
    
    df["stat_flag"] = "ok"
    
    for proj in df["cleaned_match_project"].unique():
        if pd.isna(proj): continue
        mask = (df["cleaned_match_project"] == proj) & (df["cleaned_relevant_for_valuation"] == True) & df["cleaned_price_value"].notna() & df["final_super_builtup_area"].notna()
        proj_df = df[mask].copy()
        
        if len(proj_df) >= 4:
            rates = proj_df["cleaned_price_value"] / proj_df["final_super_builtup_area"]
            Q1 = rates.quantile(0.25)
            Q3 = rates.quantile(0.75)
            IQR = Q3 - Q1
            
            if IQR == 0:
                # All rates identical - don't flag as outliers
                continue
            
            lower_bound = Q1 - 3 * IQR # 3x IQR for extreme outliers
            upper_bound = Q3 + 3 * IQR
            
            outlier_indices = proj_df[(rates < lower_bound) | (rates > upper_bound)].index
            df.loc[outlier_indices, "stat_flag"] = "outlier"
            
    return df

def data_cleaning_pipeline(
    listings: List[Dict],
    subject: Dict,
    comparables: List[Dict],
    property_type: str,
    on_progress=None
) -> Dict:
    metrics = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    is_apartment = property_type in ["apartment"]
    
    if on_progress: 
        print(f"🧹 [Data Cleaning] Starting pipeline for {len(listings)} listings...")
        on_progress("cleaning_start", "Starting data cleaning pipeline")

    # 1. Pre-process and Dedup
    for i, lst in enumerate(listings): lst["_raw_row_id"] = i
    
    listings = pre_process_normalisation(listings)
    listings, duplicate_count = deduplicate_listings(listings)
    
    if on_progress: 
        print(f"✅ [Data Cleaning] Deduplication complete. Flagged {duplicate_count} duplicates.")
        on_progress("dedup_done", f"Deduplication complete. Flagged {duplicate_count} duplicates.")

    # Only send non-duplicates to LLM to save tokens
    unique_listings = [l for l in listings if not l.get("is_duplicate")]
    
    if not unique_listings:
        return {"cleaned_listings": [], "review_listings": [], "dropped_listings": listings, "audit_stats": {}}

    # 2. Batch LLM Processing (Project-Wise)
    from collections import defaultdict
    batch_size = 10
    
    project_groups = defaultdict(list)
    for l in unique_listings:
        pname = l.get("project_name") or "Unknown"
        project_groups[pname].append(l)

    batches = []
    for p_listings in project_groups.values():
        for i in range(0, len(p_listings), batch_size):
            batches.append(p_listings[i:i + batch_size])
    
    subject_name = subject.get("project_name", "Subject")
    comp_names = [c.get("project_name", "Comp") for c in comparables]
    
    # NEW: Include location name or listings without a specific project name in the target list
    # so the LLM doesn't drop them.
    found_projects = {l.get("project_name") for l in unique_listings if l.get("project_name")}
    general_loc_name = subject.get("location_name") or subject.get("locality") or "General Locality"
    if general_loc_name in found_projects:
        comp_names.append(general_loc_name)
    
    sys_prompt = build_cleaning_system_prompt(subject_name, comp_names)
    
    llm_results = []
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(process_batch, batch, sys_prompt, metrics): i for i, batch in enumerate(batches)}
        for future in as_completed(futures):
            i = futures[future]
            if on_progress: 
                print(f"🧠 [Data Cleaning] LLM Batch {i+1}/{len(batches)} processed.")
                on_progress(f"llm_batch_{i}", f"Processed batch {i+1}/{len(batches)}")
            try:
                res = future.result()
                llm_results.extend(res)
            except Exception as e:
                logger.error(f"Error in batch {i}: {e}")

    # 3. Merge LLM output with raw rows
    df_raw = pd.DataFrame(unique_listings)
    df_llm = pd.DataFrame(llm_results)
    
    if not df_llm.empty and "raw_row_id" in df_llm.columns:
        # Prefix LLM columns
        df_llm = df_llm.add_prefix("cleaned_")
        df_llm = df_llm.rename(columns={"cleaned_raw_row_id": "_raw_row_id"})
        df_merged = pd.merge(df_raw, df_llm, on="_raw_row_id", how="left")
    else:
        df_merged = df_raw.copy()
        for col in ["match_project", "relevant_for_valuation", "price_value", "area_sqft", "area_type", "floor", "total_floors", "currency", "road_type"]:
            df_merged[f"cleaned_{col}"] = None
    
    # Fill gaps with python parses
    if "cleaned_price_value" in df_merged.columns:
        df_merged["cleaned_price_value"] = df_merged["cleaned_price_value"].fillna(df_merged.get("price_parsed_py", np.nan))
    if "cleaned_area_sqft" in df_merged.columns:
        df_merged["cleaned_area_sqft"] = df_merged["cleaned_area_sqft"].fillna(df_merged.get("area_parsed_py", np.nan))

    # 4. Apply Area Conversion
    if on_progress: 
        print(f"📐 [Data Cleaning] Applying data-driven area conversions...")
        on_progress("area_conversion", "Applying data-driven area conversions")
    df_merged = apply_area_conversion(df_merged, is_apartment)
    
    # 5. Statistical Prescreening
    if on_progress: 
        print(f"📊 [Data Cleaning] Running statistical outlier detection...")
        on_progress("stat_screen", "Running statistical outlier detection")
    df_merged = stat_prescreening(df_merged)

    # 6. Bucket Results
    if "cleaned_relevant_for_valuation" not in df_merged.columns:
        df_merged["cleaned_relevant_for_valuation"] = False
        
    df_merged["cleaned_relevant_for_valuation"] = df_merged["cleaned_relevant_for_valuation"].fillna(False).astype(bool)

    good_mask = (df_merged["cleaned_relevant_for_valuation"] == True) & (df_merged["stat_flag"] == "ok") & df_merged["cleaned_price_value"].notna() & df_merged["final_super_builtup_area"].notna()
    review_mask = (df_merged["cleaned_relevant_for_valuation"] == True) & (df_merged["stat_flag"] == "outlier")
    dropped_mask = (df_merged["cleaned_relevant_for_valuation"] == False)
    
    # Replace NaN with None to prevent invalid JSON 'NaN' tokens
    df_merged = df_merged.replace({np.nan: None})

    good_listings = df_merged[good_mask].to_dict("records")
    review_listings = df_merged[review_mask].to_dict("records")
    dropped_llm = df_merged[dropped_mask].to_dict("records")
    
    # Add back duplicates to dropped
    duplicate_rows = [l for l in listings if l.get("is_duplicate")]
    all_dropped = dropped_llm + duplicate_rows

    audit_stats = {
        "input_count": len(listings),
        "dedup_removed": duplicate_count,
        "llm_processed": len(unique_listings),
        "relevant_ok_count": len(good_listings),
        "outlier_count": len(review_listings),
        "token_usage": metrics
    }
    
    if on_progress: 
        print(f"✨ [Data Cleaning] Pipeline complete. {len(good_listings)} valid listings.")
        on_progress("cleaning_done", "Data cleaning pipeline complete")

    return {
        "cleaned_listings": good_listings,
        "review_listings": review_listings,
        "dropped_listings": all_dropped,
        "audit_stats": audit_stats
    }
