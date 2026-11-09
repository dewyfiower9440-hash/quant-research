"""LLM cost accounting and limits (Phase 3; design §12, §16-7).

Prices are the Anthropic first-party list prices per million tokens, read from the official pricing page
https://platform.claude.com/docs/en/about-claude/pricing on 2026-11-10. Thinking tokens are counted as output
tokens (they are reported inside `usage.output_tokens`; the page itself was not explicit, so this is the
conservative reading). Costs are kept in three kinds:
- `estimate_usd`: computed BEFORE a call from an upper bound of tokens (budget check);
- `usage_cost_usd`: computed AFTER a call from the reported usage x list price (what is billable);
- invoice: not available here (needs the Console / Admin API); never claimed.
Fake and replay LLMs are never billed; their "would-be" cost uses the same price table.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from quant_research.errors import AppError


class BudgetExceeded(AppError):
    """The next LLM call would break a configured limit; the Director pauses instead."""


@dataclass(frozen=True, slots=True)
class Price:
    input: float  # USD per million tokens
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.0, output=20.0, cache_write_5m=5.0, cache_write_1h=8.0, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.0, output=10.0, cache_write_5m=2.5, cache_write_1h=4.0, cache_read=0.10),
    # a server-side refusal fallback may answer with another model; the page does not say whose rates apply,
    # so billed pre-call estimates use the most expensive listed model (this one)
    "claude-fable-5-1": Price(input=10.0, output=50.0, cache_write_5m=12.5, cache_write_1h=20.0, cache_read=0.25),
}
PRICE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing (read 2026-11-10)"
PRICED_AS = "claude-opus-5-5"  # fake runs report what the same tokens would cost on the planned model


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (newer tokenizers produce ~30 % more tokens): 1 token per 3 characters."""
    return math.ceil(len(text) / 3)


def cost_usd(
    model_id: str, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0
) -> float:
    price = PRICES.get(model_id, PRICES[PRICED_AS])
    return (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
        + cache_write_tokens * price.cache_write_5m
    ) / 1e6


@dataclass
class Budget:
    """Limits; `spent_billed_usd` is cumulative over all paid runs (loaded from research.db)."""

    max_total_usd: float = 5.0
    max_cycle_usd: float = 2.5  # worst-case estimates (fallback pricing) of ~3 calls + retries; total cap binds
    max_calls_per_cycle: int = 8
    max_input_tokens: int = 50_000
    max_output_tokens: int = 8_000
    spent_billed_usd: float = 0.0
    cycle_usd: float = 0.0
    cycle_calls: int = 0

    def start_cycle(self) -> None:
        self.cycle_usd = 0.0
        self.cycle_calls = 0

    def check(self, *, model_id: str, prompt_text: str, billed: bool) -> float:
        """Pre-call estimate; raises BudgetExceeded when the call could break a limit."""
        tokens_in = estimate_tokens(prompt_text)
        if tokens_in > self.max_input_tokens:
            raise BudgetExceeded(f"prompt ~{tokens_in} tokens > per-call limit {self.max_input_tokens}")
        if self.cycle_calls + 1 > self.max_calls_per_cycle:
            raise BudgetExceeded(f"call {self.cycle_calls + 1} > {self.max_calls_per_cycle} calls per cycle")
        if billed:  # worst case over every model that could answer (fallbacks)
            estimate = max(cost_usd(m, input_tokens=tokens_in, output_tokens=self.max_output_tokens) for m in PRICES)
        else:
            estimate = cost_usd(model_id, input_tokens=tokens_in, output_tokens=self.max_output_tokens)
        if billed:
            if self.spent_billed_usd + estimate > self.max_total_usd:
                raise BudgetExceeded(
                    f"spent ${self.spent_billed_usd:.4f} + estimate ${estimate:.4f} "
                    f"> total cap ${self.max_total_usd:.2f}"
                )
            if self.cycle_usd + estimate > self.max_cycle_usd:
                raise BudgetExceeded(
                    f"cycle ${self.cycle_usd:.4f} + estimate ${estimate:.4f} > cycle cap ${self.max_cycle_usd:.2f}"
                )
        return estimate

    def record(self, *, usage_cost: float, billed: bool) -> None:
        self.cycle_calls += 1
        if billed:
            self.spent_billed_usd += usage_cost
            self.cycle_usd += usage_cost
