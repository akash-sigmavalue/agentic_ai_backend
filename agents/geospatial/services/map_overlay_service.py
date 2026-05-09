"""
Service layer for map overlay endpoints.

Provides price-momentum and village listing queries against the
same PostgreSQL database used by Project 1 (Django backend_sigma).

The queries use raw SQL so they work regardless of whether SQLAlchemy
models have been defined for the legacy Django tables.
"""

from typing import List, Dict, Any, Optional
from sqlalchemy import text
from database.geospatial.database import engine


def get_villages_for_city(city_id: int) -> List[str]:
    """Return distinct village names for a given city_id."""
    sql = text("""
        SELECT DISTINCT v.name
        FROM data_db_cgdb t
        JOIN data_db_project p ON t.project_id = p.id
        JOIN data_db_village v ON t.igr_village_id = v.id
        WHERE p.city_id = :city_id
        ORDER BY v.name
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"city_id": city_id}).fetchall()
    return [row[0] for row in rows]


def get_price_momentum(
    city_id: Optional[int] = None,
    project_id: Optional[int] = None,
    village_name: Optional[str] = None,
    year: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Return price momentum data grouped by project.

    For each project shows latest-year and previous-year average rates
    (carpet area and salable area) plus year-over-year growth percentages.
    """
    where_clauses = ["1=1"]
    params: Dict[str, Any] = {}

    if city_id is not None:
        where_clauses.append("p.city_id = :city_id")
        params["city_id"] = city_id
    if project_id is not None:
        where_clauses.append("l.project_id = :project_id")
        params["project_id"] = project_id
    if village_name is not None:
        where_clauses.append("v.name = :village_name")
        params["village_name"] = village_name
    if year is not None:
        where_clauses.append("l.latest_year = :year")
        params["year"] = year

    where_sql = " AND ".join(where_clauses)

    sql = text(f"""
    SELECT
        l.project_id,
        p.name AS project_name,
        p.lat AS latitude,
        p.lng AS longitude,
        p.city_id,
        STRING_AGG(DISTINCT v.name, ', ' ORDER BY v.name) AS village_names,
        l.latest_year AS year_latest,
        ROUND(CAST(AVG(CASE
            WHEN t.year = l.latest_year
            THEN t.transaction_rate_per_sqft_on_ca
        END) AS numeric), 2) AS avg_ca_latest,
        ROUND(CAST(AVG(CASE
            WHEN t.year = l.latest_year
            THEN t.transaction_rate_per_sqft_on_sa
        END) AS numeric), 2) AS avg_sa_latest,
        l.latest_year - 1 AS year_previous,
        ROUND(CAST(AVG(CASE
            WHEN t.year = l.latest_year - 1
            THEN t.transaction_rate_per_sqft_on_ca
        END) AS numeric), 2) AS avg_ca_previous,
        ROUND(CAST(AVG(CASE
            WHEN t.year = l.latest_year - 1
            THEN t.transaction_rate_per_sqft_on_sa
        END) AS numeric), 2) AS avg_sa_previous,
        ROUND(
            CASE
                WHEN AVG(CASE WHEN t.year = l.latest_year - 1 THEN t.transaction_rate_per_sqft_on_ca END) IS NULL
                     OR AVG(CASE WHEN t.year = l.latest_year - 1 THEN t.transaction_rate_per_sqft_on_ca END) = 0
                THEN NULL
                ELSE
                    (
                        (
                            CAST(AVG(CASE WHEN t.year = l.latest_year THEN t.transaction_rate_per_sqft_on_ca END) AS numeric)
                            - CAST(AVG(CASE WHEN t.year = l.latest_year - 1 THEN t.transaction_rate_per_sqft_on_ca END) AS numeric)
                        ) * 100.0
                    )
                    / CAST(AVG(CASE WHEN t.year = l.latest_year - 1 THEN t.transaction_rate_per_sqft_on_ca END) AS numeric)
            END
        , 2) AS growth_pct_ca,
        ROUND(
            CASE
                WHEN AVG(CASE WHEN t.year = l.latest_year - 1 THEN t.transaction_rate_per_sqft_on_sa END) IS NULL
                     OR AVG(CASE WHEN t.year = l.latest_year - 1 THEN t.transaction_rate_per_sqft_on_sa END) = 0
                THEN NULL
                ELSE
                    (
                        (
                            CAST(AVG(CASE WHEN t.year = l.latest_year THEN t.transaction_rate_per_sqft_on_sa END) AS numeric)
                            - CAST(AVG(CASE WHEN t.year = l.latest_year - 1 THEN t.transaction_rate_per_sqft_on_sa END) AS numeric)
                        ) * 100.0
                    )
                    / CAST(AVG(CASE WHEN t.year = l.latest_year - 1 THEN t.transaction_rate_per_sqft_on_sa END) AS numeric)
            END
        , 2) AS growth_pct_sa
    FROM data_db_cgdb t
    JOIN (
        SELECT
            project_id,
            MAX(year) AS latest_year
        FROM data_db_cgdb
        GROUP BY project_id
    ) l
        ON t.project_id = l.project_id
       AND t.year IN (l.latest_year, l.latest_year - 1)
    LEFT JOIN data_db_project p
        ON l.project_id = p.id
    LEFT JOIN data_db_village v
        ON t.igr_village_id = v.id
    WHERE {where_sql}
    GROUP BY
        l.project_id,
        p.name,
        p.lat,
        p.lng,
        p.city_id,
        l.latest_year
    ORDER BY l.project_id ASC;
    """)

    with engine.connect() as conn:
        result = conn.execute(sql, params)
        columns = list(result.keys())
        rows = [dict(zip(columns, row)) for row in result.fetchall()]

    return rows
