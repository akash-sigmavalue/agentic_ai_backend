import time


class AgentMetrics:
    def __init__(self):
        self.start_time = time.time()
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tools_called = 0
        self.cache_hits = 0
        self.sql_retries = 0
        # Model-wise breakdown: { "model_name": { "prompt": 0, "completion": 0, "total": 0 } }
        self.model_usage = {}
        # Tool-wise breakdown: { "tool_name": { "calls": 0, "cost_usd": 0.0 } }
        self.tool_usage = {}

    def _extract_tokens(self, usage):
        if usage is None:
            return 0, 0, 0
        if isinstance(usage, dict):
            p = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            c = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            t = int(usage.get("total_tokens", p + c) or (p + c))
            return p, c, t
        p = int(getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)) or 0)
        c = int(getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0)) or 0)
        t = int(getattr(usage, "total_tokens", p + c) or (p + c))
        return p, c, t

    def add_tokens(self, usage, model_name="unknown"):
        p, c, t = self._extract_tokens(usage)
        self.total_tokens += t
        self.prompt_tokens += p
        self.completion_tokens += c
        
        if model_name not in self.model_usage:
            self.model_usage[model_name] = {"prompt": 0, "completion": 0, "total": 0}
        
        self.model_usage[model_name]["prompt"] += p
        self.model_usage[model_name]["completion"] += c
        self.model_usage[model_name]["total"] += t
        
        return {
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": t,
            "model": model_name
        }

    def add_tool_call(self, tool_name, cost=0.0):
        self.tools_called += 1
        if tool_name not in self.tool_usage:
            self.tool_usage[tool_name] = {"calls": 0, "cost_usd": 0.0}
        self.tool_usage[tool_name]["calls"] += 1
        self.tool_usage[tool_name]["cost_usd"] += cost

    def snapshot(self) -> dict:
        # Calculate model cost dynamically based on model rates
        total_model_cost = 0.0
        for model_name, usage in self.model_usage.items():
            p = usage.get("prompt", 0)
            c = usage.get("completion", 0)
            model_lower = model_name.lower()
            if "gpt-4o" in model_lower and "mini" not in model_lower:
                # GPT-4o pricing: $5.00/1M input, $15.00/1M output
                model_cost = (p / 1_000_000) * 5.00 + (c / 1_000_000) * 15.00
            else:
                # Default to GPT-4o-mini: $0.15/1M input, $0.60/1M output
                model_cost = (p / 1_000_000) * 0.15 + (c / 1_000_000) * 0.60
            total_model_cost += model_cost

        tool_cost = sum(t["cost_usd"] for t in self.tool_usage.values())
        total_cost = total_model_cost + tool_cost

        return {
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(total_cost, 6),
            "model_breakdown": self.model_usage,
            "tool_breakdown": self.tool_usage,
        }

    def finalize(self) -> dict:
        duration = time.time() - self.start_time
        snap = self.snapshot()
        return {
            "duration_seconds": round(duration, 2),
            "total_tokens": self.total_tokens,
            "cost_usd": snap["cost_usd"],
            "tools_called": self.tools_called,
            "cache_hits": self.cache_hits,
            "sql_retries": self.sql_retries,
            "model_breakdown": self.model_usage,
            "tool_breakdown": self.tool_usage,
        }

