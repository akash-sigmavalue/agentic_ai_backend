import re
from decimal import Decimal
from sqlalchemy import text
from database.db import engine

DANGEROUS_KEYWORDS = {"insert", "update", "delete", "drop", "truncate", "alter", "create"}
DEFAULT_LIMIT = 30


class ExecutionEngine:
    def __init__(self, query_builder, max_retries: int = 3):
        self.query_builder = query_builder
        self.max_retries = max_retries

    def _validate(self, sql: str) -> None:
        clean = sql.strip().lower()
        if not (clean.startswith("select") or clean.startswith("with")):
            raise ValueError(f"Query must start with SELECT or WITH. Got: {clean[:60]}")
        # Basic multi-statement guard.
        if clean.count(";") > 1:
            raise ValueError("Only one SQL statement is allowed.")
        for kw in DANGEROUS_KEYWORDS:
            if re.search(rf"\b{kw}\b", clean):
                raise ValueError(f"Dangerous keyword detected: {kw}")

    def _strip_fences(self, sql: str) -> str:
        sql = re.sub(r"```(?:sql)?", "", sql, flags=re.IGNORECASE)
        return sql.replace("```", "").strip()

    def _ensure_limit(self, sql: str) -> str:
        match = re.search(r"\blimit\s+(\d+)\b", sql, flags=re.IGNORECASE)
        if match:
            existing_limit = int(match.group(1))
            # Only override if the existing limit is larger than our safety cap
            if existing_limit > DEFAULT_LIMIT:
                return re.sub(r"\blimit\s+\d+\b", f"LIMIT {DEFAULT_LIMIT}", sql, count=1, flags=re.IGNORECASE)
            return sql
        return f"{sql.rstrip().rstrip(';')} LIMIT {DEFAULT_LIMIT}"

    def _serialize_row(self, row_dict: dict) -> dict:
        """Convert Decimal and other non-serializable types."""
        out = {}
        for k, v in row_dict.items():
            if isinstance(v, Decimal):
                out[k] = float(v)
            else:
                out[k] = v
        return out

    def execute(self, sql: str) -> dict:
        sql = self._strip_fences(sql)
        sql = self._ensure_limit(sql)
        last_error = None
        retries_used = 0

        for attempt in range(self.max_retries):
            try:
                self._validate(sql)
                with engine.connect() as conn:
                    result = conn.execute(text(sql))
                    columns = list(result.keys())
                    rows = [self._serialize_row(dict(zip(columns, row))) for row in result.fetchall()]
                return {
                    "status": "success",
                    "data": rows,
                    "columns": columns,
                    "row_count": len(rows),
                    "retries": retries_used,
                }
            except Exception as e:
                last_error = str(e)
                retries_used += 1
                if attempt < self.max_retries - 1:
                    sql = self._strip_fences(self.query_builder.fix(sql, last_error))
                    sql = self._ensure_limit(sql)
                    continue

        return {
            "status": "error",
            "error": last_error,
            "data": [],
            "columns": [],
            "row_count": 0,
            "retries": retries_used,
        }
