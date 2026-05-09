import json

from sqlalchemy import text

from database.db import SessionLocal


def execute_sql_query(sql_query: str) -> str:
    db = SessionLocal()
    try:
        result = db.execute(text(sql_query))
        rows = result.mappings().all()
        data = [dict(row) for row in rows]
        return json.dumps(data, indent=2, default=str)
    finally:
        db.close()


def execute_sql_queries(sql_queries: list[str]) -> dict:
    db = SessionLocal()
    results = {}
    try:
        for idx, query in enumerate(sql_queries):
            result = db.execute(text(query))
            rows = result.mappings().all()
            data = [dict(row) for row in rows]
            results[f"dataset_{idx}"] = data
        return results
    finally:
        db.close()
