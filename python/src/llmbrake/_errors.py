"""Typed errors raised by llmbrake."""

from __future__ import annotations

from typing import Any, Tuple


class LLMBrakeError(Exception):
    """Base class for errors raised by llmbrake."""

    code: str = "LLMBRAKE_ERROR"


class BudgetExceededError(LLMBrakeError):
    """Raised *before* the provider is called when a customer is at or over their budget."""

    code = "LLMBRAKE_BUDGET_EXCEEDED"

    def __init__(self, customer_id: str, spent_usd: float, budget_usd: float, period: str) -> None:
        self.customer_id = customer_id
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd
        self.period = period
        super().__init__(
            f"Customer {customer_id} has used ${spent_usd:.4f} of a ${budget_usd:.2f} AI budget for {period}."
        )

    def __reduce__(self) -> Tuple[Any, ...]:
        return (type(self), (self.customer_id, self.spent_usd, self.budget_usd, self.period))


class UnknownModelPriceError(LLMBrakeError):
    """Raised before the call when the (effective) model has no entry in the price table."""

    code = "LLMBRAKE_UNKNOWN_MODEL"

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f'No price configured for model "{model}". Add it to the price table.')

    def __reduce__(self) -> Tuple[Any, ...]:
        return (type(self), (self.model,))


def is_budget_exceeded_error(e: object) -> bool:
    """True if ``e`` is a :class:`BudgetExceededError`."""
    return isinstance(e, BudgetExceededError)
