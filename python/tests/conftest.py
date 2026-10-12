from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from llmbrake import ModelPrice

PRICES = {
    "big": ModelPrice(input_per_mtok=10, output_per_mtok=30, cached_input_per_mtok=1),
    "small": ModelPrice(input_per_mtok=1, output_per_mtok=2),
}


def fixed_now() -> datetime:
    return datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def fake_create(calls: List[Dict[str, Any]]):
    """Fake OpenAI-compatible create(): echoes the model, fixed usage of 100k in / 100k out."""

    def create(**params: Any) -> Dict[str, Any]:
        calls.append(params)
        return {
            "model": params["model"],
            "usage": {"prompt_tokens": 100_000, "completion_tokens": 100_000},
            "choices": [],
        }

    return create


def fake_acreate(calls: List[Dict[str, Any]]):
    async def create(**params: Any) -> Dict[str, Any]:
        calls.append(params)
        return {
            "model": params["model"],
            "usage": {"prompt_tokens": 100_000, "completion_tokens": 100_000},
            "choices": [],
        }

    return create


@pytest.fixture
def prices():
    return PRICES
