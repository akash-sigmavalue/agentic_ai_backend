"""
Cost Evaluation Framework for Project SQL Agent  —  v1.0
========================================================

Measures the second evaluation layer for the project ReAct agent:

  - Token consumption: prompt, completion, total tokens
  - Estimated LLM cost: configurable per-model input/output prices
  - Latency: per case, average, p50, p95
  - ReAct efficiency: iterations used and token cost per successful answer
  - Budget compliance: fail cases that exceed your configured limits

This file intentionally reuses the functional test cases from eval_functional.py
so cost and accuracy can be compared on the same question set.

Examples
--------
  # Cheap smoke test with mock DB rows
  python backend/agent/project/eval_cost.py --mode mock --cases PC_001 PC_002

  # Full run against DATABASE_URL
  python backend/agent/project/eval_cost.py --mode full

  # Compare models on the same cases
  python backend/agent/project/eval_cost.py --mode mock --models gpt-4.1-mini gpt-4.1

  # Provide current pricing explicitly, per 1M tokens
  python backend/agent/project/eval_cost.py --input-price 0.40 --output-price 1.60
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from openai import OpenAI
except ImportError:
    print("[ERROR] openai not installed. Run: pip install openai")
    sys.exit(1)

try:
    from langsmith import traceable
    HAS_LANGSMITH = True
except ImportError:
    HAS_LANGSMITH = False
    traceable = None

try:
    from agents.data_retrieval_project.eval_functional import TEST_CASES, _build_db_executor
    from agents.data_retrieval_project.query_builder import (
        IntentExtractor,
        QueryResult,
        ProjectQueryBuilder,
    )
except ImportError as exc:
    print(f"[ERROR] Could not import project agent/eval code: {exc}")
    sys.exit(1)


# Standard text-token prices per 1M tokens.
# Source checked against official OpenAI pricing pages on 2026-04-30.
# Keep --input-price/--output-price available because provider pricing can change.
DEFAULT_PRICES_PER_1M: dict[str, dict[str, float]] = {
    "gpt-5.5": {"input": 5.00, "output": 30.00},
    "gpt-5.4": {"input": 2.50, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "gpt-5.2": {"input": 1.75, "output": 14.00},
    "gpt-5.1": {"input": 1.25, "output": 10.00},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-5-nano": {"input": 0.05, "output": 0.40},
    "gpt-5.2-chat-latest": {"input": 1.75, "output": 14.00},
    "gpt-5.1-chat-latest": {"input": 1.25, "output": 10.00},
    "gpt-5-chat-latest": {"input": 1.25, "output": 10.00},
    "gpt-5.2-codex": {"input": 1.75, "output": 14.00},
    "gpt-5.1-codex-max": {"input": 1.25, "output": 10.00},
    "gpt-5.1-codex": {"input": 1.25, "output": 10.00},
    "gpt-5-codex": {"input": 1.25, "output": 10.00},
    "gpt-5.2-pro": {"input": 21.00, "output": 168.00},
    "gpt-5-pro": {"input": 15.00, "output": 120.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-2024-05-13": {"input": 5.00, "output": 15.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-realtime": {"input": 4.00, "output": 16.00},
    "gpt-realtime-mini": {"input": 0.60, "output": 2.40},
}

MODEL_PRICE_ALIASES: dict[str, str] = {
    "chatgpt-4o-latest": "gpt-4o",
}


@dataclass
class CostBudget:
    """Per-case budget thresholds."""

    max_total_tokens: int = 20_000
    max_latency_ms: int = 45_000
    max_iterations: int = 3
    max_cost_usd: float = 0.05


@dataclass
class TokenUsage:
    """Normalized token usage from OpenAI response.usage objects or dicts."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage: Any) -> None:
        normalized = normalize_usage(usage)
        self.prompt_tokens += normalized.prompt_tokens
        self.completion_tokens += normalized.completion_tokens
        self.total_tokens += normalized.total_tokens


@dataclass
class CostEvalResult:
    test_id: str
    query: str
    model: str
    passed_budget: bool
    agent_success: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    iterations_used: int
    rows_returned: int
    cost_per_successful_answer_usd: float
    tokens_per_iteration: float
    issues: list[str] = field(default_factory=list)
    sql: str = ""
    error: str | None = None


@dataclass
class CostEvalReport:
    timestamp: str
    mode: str
    models: list[str]
    total_cases: int
    budget: CostBudget
    results: list[CostEvalResult]
    summary_by_model: dict[str, dict[str, float]]


def dataclass_to_dict(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {key: dataclass_to_dict(value) for key, value in obj.__dict__.items()}
    if isinstance(obj, list):
        return [dataclass_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {key: dataclass_to_dict(value) for key, value in obj.items()}
    return obj


def normalize_usage(usage: Any) -> TokenUsage:
    """Accept OpenAI usage objects, dicts, or None and return TokenUsage."""
    if usage is None:
        return TokenUsage()
    if isinstance(usage, dict):
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        total = int(usage.get("total_tokens") or prompt + completion)
        return TokenUsage(prompt, completion, total)

    prompt = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
    completion = int(
        getattr(usage, "completion_tokens", 0)
        or getattr(usage, "output_tokens", 0)
        or 0
    )
    total = int(getattr(usage, "total_tokens", 0) or prompt + completion)
    return TokenUsage(prompt, completion, total)


def get_model_prices_per_1m(model: str) -> dict[str, float]:
    """Return input/output prices for known OpenAI text models."""
    normalized_model = MODEL_PRICE_ALIASES.get(model, model)
    if normalized_model in DEFAULT_PRICES_PER_1M:
        return DEFAULT_PRICES_PER_1M[normalized_model]

    # Handle dated snapshots such as gpt-4.1-mini-2025-04-14.
    parts = normalized_model.rsplit("-", 3)
    if len(parts) == 4 and all(part.isdigit() for part in parts[-3:]):
        base_model = parts[0]
        if base_model in DEFAULT_PRICES_PER_1M:
            return DEFAULT_PRICES_PER_1M[base_model]

    known_models = ", ".join(sorted(DEFAULT_PRICES_PER_1M))
    raise ValueError(
        f"No built-in price found for model '{model}'. "
        "Add it to DEFAULT_PRICES_PER_1M or pass --input-price and --output-price. "
        f"Known models: {known_models}"
    )


def estimate_cost_usd(
    usage: TokenUsage,
    model: str,
    input_price_per_1m: float | None = None,
    output_price_per_1m: float | None = None,
) -> float:
    """Estimate cost from input/output token usage and per-1M token prices."""
    env_input_price = os.getenv("EVAL_INPUT_PRICE_PER_1M")
    env_output_price = os.getenv("EVAL_OUTPUT_PRICE_PER_1M")
    needs_defaults = (
        input_price_per_1m is None
        and env_input_price is None
        or output_price_per_1m is None
        and env_output_price is None
    )
    defaults = get_model_prices_per_1m(model) if needs_defaults else {}
    input_price = (
        input_price_per_1m
        if input_price_per_1m is not None
        else float(env_input_price if env_input_price is not None else defaults["input"])
    )
    output_price = (
        output_price_per_1m
        if output_price_per_1m is not None
        else float(env_output_price if env_output_price is not None else defaults["output"])
    )
    return (
        usage.prompt_tokens * input_price / 1_000_000
        + usage.completion_tokens * output_price / 1_000_000
    )


class CostEvaluator:
    """Runs the agent and scores runtime/cost behavior against budgets."""

    def __init__(
        self,
        client: OpenAI,
        db_executor: Callable[[str], list[dict]],
        model: str,
        budget: CostBudget,
        input_price_per_1m: float | None = None,
        output_price_per_1m: float | None = None,
    ) -> None:
        self.client = client
        self.db_executor = db_executor
        self.model = model
        self.budget = budget
        self.input_price_per_1m = input_price_per_1m
        self.output_price_per_1m = output_price_per_1m

    def evaluate(self, test_case: dict) -> CostEvalResult:
        query = test_case["query"]
        usage = TokenUsage()
        t0 = time.monotonic()
        issues: list[str] = []

        try:
            extractor = IntentExtractor(client=self.client, model=self.model)
            intent = extractor.extract(query)
            usage.add(extractor.last_usage)

            builder = ProjectQueryBuilder(
                client=self.client,
                db_executor=self.db_executor,
                model=self.model,
            )
            result: QueryResult = builder.run(intent)
            usage.add(result.usage)

            latency_ms = int((time.monotonic() - t0) * 1000)
            estimated_cost = estimate_cost_usd(
                usage,
                self.model,
                input_price_per_1m=self.input_price_per_1m,
                output_price_per_1m=self.output_price_per_1m,
            )

            if usage.total_tokens > self.budget.max_total_tokens:
                issues.append(
                    f"Token budget exceeded: {usage.total_tokens} > {self.budget.max_total_tokens}"
                )
            if latency_ms > self.budget.max_latency_ms:
                issues.append(
                    f"Latency budget exceeded: {latency_ms}ms > {self.budget.max_latency_ms}ms"
                )
            if result.iterations > self.budget.max_iterations:
                issues.append(
                    f"Iteration budget exceeded: {result.iterations} > {self.budget.max_iterations}"
                )
            if estimated_cost > self.budget.max_cost_usd:
                issues.append(
                    f"Cost budget exceeded: ${estimated_cost:.6f} > ${self.budget.max_cost_usd:.6f}"
                )

            return CostEvalResult(
                test_id=test_case["id"],
                query=query,
                model=self.model,
                passed_budget=not issues,
                agent_success=result.success,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                estimated_cost_usd=estimated_cost,
                latency_ms=latency_ms,
                iterations_used=result.iterations,
                rows_returned=len(result.rows),
                cost_per_successful_answer_usd=estimated_cost if result.success else 0.0,
                tokens_per_iteration=usage.total_tokens / max(result.iterations, 1),
                issues=issues,
                sql=result.sql,
                error=result.error,
            )

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CostEvalResult(
                test_id=test_case["id"],
                query=query,
                model=self.model,
                passed_budget=False,
                agent_success=False,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                estimated_cost_usd=estimate_cost_usd(
                    usage,
                    self.model,
                    input_price_per_1m=self.input_price_per_1m,
                    output_price_per_1m=self.output_price_per_1m,
                ),
                latency_ms=latency_ms,
                iterations_used=0,
                rows_returned=0,
                cost_per_successful_answer_usd=0.0,
                tokens_per_iteration=0.0,
                issues=[f"Agent raised exception: {exc}"],
                error=str(exc),
            )


def build_mock_executor() -> Callable[[str], list[dict]]:
    """Cheap deterministic executor for cost smoke tests without a live DB."""

    def executor(sql: str) -> list[dict]:
        return [
            {
                "location_name": "Baner",
                "project_count": 24,
                "total_units": 1800,
                "available_units": 420,
                "booking_rate": 76.67,
                "plot_area_sqft": 55_000,
            },
            {
                "location_name": "Hinjewadi",
                "project_count": 18,
                "total_units": 1320,
                "available_units": 310,
                "booking_rate": 76.52,
                "plot_area_sqft": 47_500,
            },
        ]

    return executor


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_values = sorted(values)
    index = (len(sorted_values) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def summarize(results: list[CostEvalResult]) -> dict[str, dict[str, float]]:
    by_model: dict[str, list[CostEvalResult]] = {}
    for result in results:
        by_model.setdefault(result.model, []).append(result)

    summary: dict[str, dict[str, float]] = {}
    for model, rows in by_model.items():
        latencies = [r.latency_ms for r in rows]
        tokens = [r.total_tokens for r in rows]
        costs = [r.estimated_cost_usd for r in rows]
        successful_costs = [
            r.estimated_cost_usd for r in rows if r.agent_success
        ]

        summary[model] = {
            "cases": len(rows),
            "budget_pass_rate": sum(r.passed_budget for r in rows) / len(rows),
            "agent_success_rate": sum(r.agent_success for r in rows) / len(rows),
            "avg_total_tokens": statistics.mean(tokens) if tokens else 0.0,
            "p95_total_tokens": percentile(tokens, 0.95),
            "avg_latency_ms": statistics.mean(latencies) if latencies else 0.0,
            "p50_latency_ms": percentile(latencies, 0.50),
            "p95_latency_ms": percentile(latencies, 0.95),
            "avg_cost_usd": statistics.mean(costs) if costs else 0.0,
            "total_cost_usd": sum(costs),
            "avg_cost_per_success_usd": (
                statistics.mean(successful_costs) if successful_costs else 0.0
            ),
            "avg_iterations": statistics.mean([r.iterations_used for r in rows]),
        }
    return summary


def print_report(report: CostEvalReport) -> None:
    sep = "=" * 72
    print(f"\n{sep}")
    print(f" COST EVALUATION REPORT")
    print(f" {report.timestamp} | mode={report.mode} | cases={report.total_cases}")
    print(f"{sep}")

    for model, summary in report.summary_by_model.items():
        print(f"\nModel: {model}")
        print(f"  Budget pass rate : {summary['budget_pass_rate']:.0%}")
        print(f"  Agent success    : {summary['agent_success_rate']:.0%}")
        print(f"  Avg tokens       : {summary['avg_total_tokens']:.0f}")
        print(f"  P95 tokens       : {summary['p95_total_tokens']:.0f}")
        print(f"  Avg latency      : {summary['avg_latency_ms']:.0f}ms")
        print(f"  P95 latency      : {summary['p95_latency_ms']:.0f}ms")
        print(f"  Avg cost         : ${summary['avg_cost_usd']:.6f}")
        print(f"  Total cost       : ${summary['total_cost_usd']:.6f}")
        print(f"  Avg iterations   : {summary['avg_iterations']:.2f}")

        for result in [r for r in report.results if r.model == model]:
            status = "PASS" if result.passed_budget else "FAIL"
            print(
                f"  {status} {result.test_id:<7} "
                f"tokens={result.total_tokens:<6} "
                f"cost=${result.estimated_cost_usd:.6f} "
                f"latency={result.latency_ms}ms "
                f"iters={result.iterations_used} "
                f"success={result.agent_success}"
            )
            for issue in result.issues[:3]:
                print(f"        - {issue}")
    print(f"{sep}\n")


def save_report_json(report: CostEvalReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(dataclass_to_dict(report), file, indent=2, default=str)
    print(f"[Report] Saved to {path}")


def evaluate_cost_case(evaluator: CostEvaluator, test_case: dict) -> CostEvalResult:
    return evaluator.evaluate(test_case)


if HAS_LANGSMITH:
    @traceable(
        name="project_cost_eval_case",
        run_type="chain",
    )
    def trace_cost_case(evaluator: CostEvaluator, test_case: dict) -> dict:
        return dataclass_to_dict(evaluator.evaluate(test_case))

    def evaluate_cost_case(evaluator: CostEvaluator, test_case: dict) -> CostEvalResult:
        return CostEvalResult(**trace_cost_case(evaluator, test_case))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cost evaluation for project SQL agent"
    )
    parser.add_argument("--mode", choices=["mock", "full"], default="mock")
    parser.add_argument(
        "--models",
        nargs="+",
        default=[os.getenv("EVAL_MODEL", "gpt-4.1-mini")],
        help="One or more models to evaluate on the same cases.",
    )
    parser.add_argument("--cases", nargs="+", help="Specific case IDs to run.")
    parser.add_argument("--save", default="project_cost_eval_report.json")
    parser.add_argument("--max-total-tokens", type=int, default=20_000)
    parser.add_argument("--max-latency-ms", type=int, default=45_000)
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--max-cost-usd", type=float, default=0.05)
    parser.add_argument(
        "--langsmith",
        action="store_true",
        help="Trace each cost-eval case to LangSmith. Requires LANGSMITH_API_KEY.",
    )
    parser.add_argument(
        "--langsmith-project",
        default=os.getenv("LANGSMITH_PROJECT", "project-sql-cost-eval"),
        help="LangSmith project name for --langsmith runs.",
    )
    parser.add_argument(
        "--input-price",
        type=float,
        default=None,
        help="Input token price per 1M tokens. Overrides built-in/env pricing.",
    )
    parser.add_argument(
        "--output-price",
        type=float,
        default=None,
        help="Output token price per 1M tokens. Overrides built-in/env pricing.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("[ERROR] Set OPENAI_API_KEY before running cost evaluation.")
        sys.exit(1)

    if args.langsmith:
        if not HAS_LANGSMITH:
            print("[ERROR] pip install langsmith to use --langsmith flag.")
            sys.exit(1)
        langsmith_api_key = os.getenv("LANGSMITH_API_KEY", "")
        if not langsmith_api_key:
            print("[ERROR] LANGSMITH_API_KEY not set.")
            sys.exit(1)
        os.environ["LANGCHAIN_API_KEY"] = langsmith_api_key
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_PROJECT"] = args.langsmith_project
        print(f"[LangSmith] Tracing cost evaluation to project: {args.langsmith_project}")

    cases = TEST_CASES
    if args.cases:
        wanted = set(args.cases)
        cases = [case for case in TEST_CASES if case["id"] in wanted]
        missing = sorted(wanted - {case["id"] for case in cases})
        if missing:
            print(f"[ERROR] Unknown case IDs: {missing}")
            sys.exit(1)

    if args.mode == "full":
        database_url = os.getenv("DATABASE_URL", "postgresql://AkashAtSigma:EbRoot_sigma6@localhost:5432/pipeline_one_db1_db2")
        if not database_url:
            print("[ERROR] --mode full requires DATABASE_URL.")
            sys.exit(1)
        db_executor = _build_db_executor(database_url)
    else:
        db_executor = build_mock_executor()

    budget = CostBudget(
        max_total_tokens=args.max_total_tokens,
        max_latency_ms=args.max_latency_ms,
        max_iterations=args.max_iterations,
        max_cost_usd=args.max_cost_usd,
    )
    client = OpenAI(api_key=api_key)

    all_results: list[CostEvalResult] = []
    print(
        f"[CostEval] Running {len(cases)} case(s) in {args.mode} mode "
        f"for model(s): {', '.join(args.models)}"
    )

    for model in args.models:
        evaluator = CostEvaluator(
            client=client,
            db_executor=db_executor,
            model=model,
            budget=budget,
            input_price_per_1m=args.input_price,
            output_price_per_1m=args.output_price,
        )
        for index, test_case in enumerate(cases, 1):
            print(f"[{model}] [{index}/{len(cases)}] {test_case['id']} - {test_case['query'][:70]}")
            all_results.append(evaluate_cost_case(evaluator, test_case))

    report = CostEvalReport(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        mode=args.mode,
        models=args.models,
        total_cases=len(cases),
        budget=budget,
        results=all_results,
        summary_by_model=summarize(all_results),
    )
    print_report(report)
    save_report_json(report, args.save)


if __name__ == "__main__":
    main()
