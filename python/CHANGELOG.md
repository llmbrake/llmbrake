# Changelog

All notable changes to the Python package are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First Python release, a port of the TypeScript `llmbrake` 0.1.0.

### Added

- `create_guard()` returning a `Guard` with `wrap` / `wrap_async`, `check` / `acheck` and `status` / `astatus`: per-customer monthly USD budgets for OpenAI-compatible `chat.completions.create` calls, sync and async (`openai.OpenAI` and `openai.AsyncOpenAI`; async clients are auto-detected by `wrap`).
- Auto-downgrade to cheaper models past a configurable fraction of the budget (`downgrade_at`, `downgrade_map`).
- Hard cap: typed `BudgetExceededError` (`LLMBRAKE_BUDGET_EXCEEDED`) raised before the provider is called; `UnknownModelPriceError` (`LLMBRAKE_UNKNOWN_MODEL`); common base `LLMBrakeError`.
- Cost calculation from token usage, including cached input tokens, with a read-only default price table (`DEFAULT_PRICES`, sources cited, needs verification) and `compute_cost_usd` / `lookup_price` helpers.
- Streaming support: sets `stream_options.include_usage` and records cost when the stream ends, is closed, or the loop exits early (`GuardedStream` / `GuardedAsyncStream` proxies).
- `UsageStore` / `AsyncUsageStore` protocols, a thread-safe in-memory `MemoryStore`, and optional `llmbrake.redis_store.RedisStore` / `AsyncRedisStore` (`pip install "llmbrake[redis]"`); fail-open by default.
- `on_usage` hook emitting metadata-only `UsageEvent`s (sync or async).
- Validation: invalid budgets (NaN, negative, non-numeric), an out-of-range `downgrade_at`, non-numeric store values, and async stores/providers used from sync calls are rejected instead of silently disabling the cap.
