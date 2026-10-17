"""Model price table and cost calculation.

Default price table, USD per 1M tokens, standard (non-batch, short-context) tier.

.. warning::
   Provider prices change often; check them before relying on a cap in production.
   Values were last checked on 2026-10-17 against:

   - OpenAI:    https://developers.openai.com/api/docs/pricing
   - Anthropic: https://docs.anthropic.com/en/docs/about-claude/pricing

   Anthropic IDs for the current generation were checked against the models overview page (2026-10-17).
   Claude Haiku 5.5 is priced at its prompts-up-to-100K tier.
   Long-context, batch, regional/data-residency uplifts and fast/priority tiers are NOT modeled.
   Override or extend via ``create_guard(prices={**DEFAULT_PRICES, **yours})``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

__all__ = ["ModelPrice", "PriceTable", "DEFAULT_PRICES", "compute_cost_usd", "lookup_price"]


@dataclass(frozen=True)
class ModelPrice:
    """Prices in USD per 1M tokens."""

    #: USD per 1M uncached input tokens
    input_per_mtok: float
    #: USD per 1M output tokens
    output_per_mtok: float
    #: USD per 1M cached input tokens (defaults to ``input_per_mtok`` if None)
    cached_input_per_mtok: Optional[float] = None
    #: Where the number came from
    source: Optional[str] = None


PriceTable = Mapping[str, ModelPrice]

_OPENAI = "https://developers.openai.com/api/docs/pricing (checked 2026-10-17, verify for production)"
_ANTHROPIC = "https://docs.anthropic.com/en/docs/about-claude/pricing (checked 2026-10-17, verify for production)"

#: Read-only default table. Copy it to extend: ``{**DEFAULT_PRICES, "my-model": ModelPrice(1, 3)}``.
DEFAULT_PRICES: PriceTable = MappingProxyType(
    {
        "gpt-6-astra": ModelPrice(10.0, 50.0, cached_input_per_mtok=1.0, source=_OPENAI),
        "gpt-6-sol": ModelPrice(2.0, 10.0, cached_input_per_mtok=0.2, source=_OPENAI),
        "gpt-6.1-sol": ModelPrice(2.0, 10.0, cached_input_per_mtok=0.1, source=_OPENAI),
        "gpt-6-luna": ModelPrice(0.1, 0.5, cached_input_per_mtok=0.01, source=_OPENAI),
        "gpt-5.6-sol": ModelPrice(4.0, 20.0, cached_input_per_mtok=0.4, source=_OPENAI),
        "gpt-5.6-terra": ModelPrice(2.0, 12.0, cached_input_per_mtok=0.2, source=_OPENAI),
        "gpt-5.6-luna": ModelPrice(0.2, 1.2, cached_input_per_mtok=0.02, source=_OPENAI),
        "gpt-5.5": ModelPrice(5.0, 30.0, cached_input_per_mtok=0.5, source=_OPENAI),
        "gpt-5.4": ModelPrice(2.5, 15.0, cached_input_per_mtok=0.25, source=_OPENAI),
        "gpt-5.4-mini": ModelPrice(0.75, 4.5, cached_input_per_mtok=0.075, source=_OPENAI),
        "gpt-5.4-nano": ModelPrice(0.2, 1.25, cached_input_per_mtok=0.02, source=_OPENAI),
        "gpt-4.1-mini": ModelPrice(0.4, 1.6, cached_input_per_mtok=0.1, source=_OPENAI),
        "gpt-4o-mini": ModelPrice(0.15, 0.6, cached_input_per_mtok=0.075, source=_OPENAI),
        "claude-fable-5-1": ModelPrice(10.0, 50.0, cached_input_per_mtok=0.25, source=_ANTHROPIC),
        "claude-opus-5-5": ModelPrice(4.0, 20.0, cached_input_per_mtok=0.2, source=_ANTHROPIC),
        "claude-sonnet-5-5": ModelPrice(2.0, 10.0, cached_input_per_mtok=0.1, source=_ANTHROPIC),
        "claude-haiku-5-5": ModelPrice(0.1, 0.5, cached_input_per_mtok=0.01, source=_ANTHROPIC),
        "claude-opus-4-8": ModelPrice(5.0, 25.0, cached_input_per_mtok=0.5, source=_ANTHROPIC),
        "claude-sonnet-5": ModelPrice(2.0, 10.0, cached_input_per_mtok=0.2, source=_ANTHROPIC),
        "claude-haiku-4-5": ModelPrice(1.0, 5.0, cached_input_per_mtok=0.1, source=_ANTHROPIC),
    }
)

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from a dict or an object (e.g. an openai pydantic model)."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _num(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return 0


def usage_tokens(usage: Any) -> Tuple[float, float, float]:
    """(prompt_tokens, completion_tokens, cached_tokens) from an OpenAI-style usage dict/object."""
    prompt = _num(_field(usage, "prompt_tokens"))
    completion = _num(_field(usage, "completion_tokens"))
    cached = _num(_field(_field(usage, "prompt_tokens_details"), "cached_tokens"))
    return prompt, completion, cached


def lookup_price(model: str, prices: PriceTable) -> Optional[ModelPrice]:
    """Exact match first, then strips a trailing date snapshot like ``-2025-04-14``."""
    p = prices.get(model)
    if p is not None:
        return p
    return prices.get(_DATE_SUFFIX.sub("", model))


def compute_cost_usd(model: str, usage: Any, prices: PriceTable) -> Optional[float]:
    """USD cost of one call, or None if the model is not in the table.

    ``usage`` may be a dict or an object with OpenAI chat-completions usage fields
    (``prompt_tokens``, ``completion_tokens``, ``prompt_tokens_details.cached_tokens``), or None.
    """
    p = lookup_price(model, prices)
    if p is None:
        return None
    prompt, out, cached = usage_tokens(usage)
    cached = min(cached, prompt)
    uncached = prompt - cached
    cached_rate = p.input_per_mtok if p.cached_input_per_mtok is None else p.cached_input_per_mtok
    return (uncached * p.input_per_mtok + cached * cached_rate + out * p.output_per_mtok) / 1_000_000
