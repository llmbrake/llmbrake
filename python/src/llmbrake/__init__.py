"""llmbrake: per-customer monthly USD budgets for OpenAI-compatible chat calls.

Track cost, auto-downgrade near the cap, block at the cap. https://llmbrake.com
"""

from ._errors import BudgetExceededError, LLMBrakeError, UnknownModelPriceError, is_budget_exceeded_error
from ._guard import BudgetStatus, CallContext, Decision, Guard, UsageEvent, create_guard, period_of
from ._prices import DEFAULT_PRICES, ModelPrice, PriceTable, compute_cost_usd, lookup_price
from ._store import AsyncUsageStore, MemoryStore, UsageStore
from ._stream import GuardedAsyncStream, GuardedStream
from ._version import __version__

__all__ = [
    "__version__",
    "create_guard",
    "Guard",
    "CallContext",
    "UsageEvent",
    "Decision",
    "BudgetStatus",
    "period_of",
    "UsageStore",
    "AsyncUsageStore",
    "MemoryStore",
    "GuardedStream",
    "GuardedAsyncStream",
    "DEFAULT_PRICES",
    "ModelPrice",
    "PriceTable",
    "compute_cost_usd",
    "lookup_price",
    "LLMBrakeError",
    "BudgetExceededError",
    "UnknownModelPriceError",
    "is_budget_exceeded_error",
]
