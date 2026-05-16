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

    def _extract_tokens(self, usage):
        if usage is None:
            return 0, 0, 0
        if isinstance(usage, dict):
            p = int(usage.get("prompt_tokens", 0) or 0)
            c = int(usage.get("completion_tokens", 0) or 0)
            t = int(usage.get("total_tokens", p + c) or (p + c))
            return p, c, t
        p = int(getattr(usage, "prompt_tokens", 0) or 0)
        c = int(getattr(usage, "completion_tokens", 0) or 0)
        t = int(getattr(usage, "total_tokens", p + c) or (p + c))
        return p, c, t

    def add_tokens(self, usage):
        p, c, t = self._extract_tokens(usage)
        self.total_tokens += t
        self.prompt_tokens += p
        self.completion_tokens += c
        return {
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": t,
        }

    def snapshot(self) -> dict:
        cost = (
            (self.prompt_tokens / 1_000_000) * 0.15
            + (self.completion_tokens / 1_000_000) * 0.60
        )
        return {
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(cost, 6),
        }

    def finalize(self) -> dict:
        duration = time.time() - self.start_time
        cost = (
            (self.prompt_tokens / 1_000_000) * 0.15
            + (self.completion_tokens / 1_000_000) * 0.60
        )
        return {
            "duration_seconds": round(duration, 2),
            "total_tokens": self.total_tokens,
            "cost_usd": round(cost, 6),
            "tools_called": self.tools_called,
            "cache_hits": self.cache_hits,
            "sql_retries": self.sql_retries,
        }
