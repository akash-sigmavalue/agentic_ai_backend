from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from openai import OpenAI
from sqlalchemy import text

from agents.data_retrieval_portfolio.prompts import ANSWER_PROMPT, SQL_BUILD_PROMPT, SQL_FIX_PROMPT
from agents.data_retrieval_portfolio.schema import PORTFOLIO_QUERY_SCHEMA
from core.config import settings
from database.portfolio.db import engine


DANGEROUS_KEYWORDS = {"insert", "update", "delete", "drop", "truncate", "alter", "create", "grant", "revoke"}
DEFAULT_LIMIT = 30


def clean_sql(sql: str) -> str:
    sql = sql.strip()
    sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip().rstrip(";")


def validate_select_only(sql: str) -> str:
    clean = sql.strip().lower()
    if not (clean.startswith("select") or clean.startswith("with")):
        raise ValueError(f"Query must start with SELECT or WITH. Got: {sql[:80]}")
    if clean.count(";") > 0:
        raise ValueError("Only one SQL statement is allowed.")
    for keyword in DANGEROUS_KEYWORDS:
        if re.search(rf"\b{keyword}\b", clean):
            raise ValueError(f"Dangerous SQL keyword detected: {keyword}")
    return sql


def ensure_limit(sql: str) -> str:
    if re.search(r"\blimit\s+\d+\b", sql, flags=re.IGNORECASE):
        return sql
    if re.search(r"\b(count|sum|avg|min|max)\s*\(", sql, flags=re.IGNORECASE) and not re.search(
        r"\bgroup\s+by\b", sql, flags=re.IGNORECASE
    ):
        return sql
    return f"{sql} LIMIT {DEFAULT_LIMIT}"


def serialize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


class PortfolioQueryBuilder:
    def __init__(self, client: OpenAI, model: str | None = None, max_retries: int = 2):
        self.client = client
        self.model = model or settings.OPENAI_MODEL
        self.max_retries = max_retries
        self.last_usage = None

    def build_sql(self, question: str, history: list[dict] | None = None) -> str:
        history_text = json.dumps((history or [])[-6:], indent=2, default=str)
        prompt = SQL_BUILD_PROMPT.format(
            schema=PORTFOLIO_QUERY_SCHEMA,
            history=history_text,
            question=question,
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "You generate safe schema-grounded PostgreSQL SELECT queries for portfolio data. Return SQL only.",
                },
                {"role": "user", "content": prompt},
            ],
            timeout=30,
        )
        self.last_usage = response.usage
        sql = clean_sql(response.choices[0].message.content or "")
        return validate_select_only(ensure_limit(sql))

    def fix_sql(self, question: str, sql: str, error: str) -> str:
        prompt = SQL_FIX_PROMPT.format(
            schema=PORTFOLIO_QUERY_SCHEMA,
            question=question,
            sql=sql,
            error=error,
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You fix PostgreSQL SELECT queries. Return corrected SQL only."},
                {"role": "user", "content": prompt},
            ],
            timeout=30,
        )
        self.last_usage = response.usage
        fixed = clean_sql(response.choices[0].message.content or "")
        return validate_select_only(ensure_limit(fixed))

    def execute_sql(self, sql: str) -> dict:
        sql = validate_select_only(ensure_limit(clean_sql(sql)))
        with engine.connect() as connection:
            result = connection.execute(text(sql))
            columns = list(result.keys())
            rows = [
                {column: serialize_value(value) for column, value in zip(columns, row)}
                for row in result.fetchall()
            ]
        return {
            "status": "success",
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }

    def run(self, question: str, history: list[dict] | None = None) -> dict:
        sql = self.build_sql(question, history)
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                db_result = self.execute_sql(sql)
                return {"status": "success", "sql": sql, "db_result": db_result, "error": None}
            except Exception as exc:
                last_error = str(exc)
                if attempt >= self.max_retries:
                    break
                sql = self.fix_sql(question, sql, last_error)
        return {
            "status": "error",
            "sql": sql,
            "db_result": {"status": "error", "columns": [], "rows": [], "row_count": 0},
            "error": last_error,
        }

    def summarize(self, question: str, sql: str, rows: list[dict]) -> str:
        prompt = ANSWER_PROMPT.format(
            question=question,
            sql=sql,
            rows=json.dumps(rows[:30], indent=2, default=str),
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You answer portfolio analytics questions from SQL rows."},
                {"role": "user", "content": prompt},
            ],
            timeout=30,
        )
        self.last_usage = response.usage
        return (response.choices[0].message.content or "").strip()
