# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.1] - 2026-10-17

### Changed

- Default price table re-checked against the OpenAI and Anthropic pricing pages on 2026-10-17 (all existing entries unchanged) and extended with current models: `gpt-6-astra`, `gpt-6-sol`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` (TypeScript and Python). Previously these threw `UnknownModelPriceError` by default.

## [0.2.0] - 2026-10-12

### Added

- `HostedStore`: shared per-customer counters in LLMBrake Cloud (one API key, every server). Dashboard budget overrides apply on the next call.
- `ReservingStore` interface (`reserve` / `settle`), detected automatically by the guard: wrapped calls reserve an estimated cost atomically before the provider call and settle the real cost after it, bounding concurrent overshoot to about one call. Failed provider calls (and unknown downgrade targets) release the hold.
- `estimateUsd` option and exported `estimateCallUsd()` used to size reservations.

- `examples/` with tested integrations for the Vercel AI SDK (`generateText`/`streamText`), OpenRouter and LangChain.js, run in CI on Node 22 against mock providers.
- Python port in `python/` (`llmbrake` 0.1.0 for Python, not yet on PyPI) with its own CI workflow. See [python/CHANGELOG.md](python/CHANGELOG.md).

### Changed

- Docs: install from npm (`npm install llmbrake`) and add an npm version badge.
- Build on git install: a `prepare` script builds `dist/`, so `npm install github:llmbrake/llmbrake` works.

## [0.1.0] - 2026-10-11

### Added

- `createGuard()` with `wrap`, `check` and `status`: per-customer monthly USD budgets for OpenAI-compatible `chat.completions.create` calls.
- Auto-downgrade to cheaper models past a configurable fraction of the budget (`downgradeAt`, `downgradeMap`).
- Hard cap: typed `BudgetExceededError` (`LLMBRAKE_BUDGET_EXCEEDED`) thrown before the provider is called.
- Cost calculation from token usage, including cached input tokens, with a default price table (`DEFAULT_PRICES`, sources cited, needs verification) and `computeCostUsd` / `lookupPrice` helpers.
- Streaming support: sets `stream_options.include_usage` and records cost when the stream ends.
- Pluggable `UsageStore` interface with an in-memory `MemoryStore`; fail-open by default.
- `onUsage` hook emitting metadata-only usage events.
- Validation: invalid budgets (NaN or negative), an out-of-range `downgradeAt`, and non-numeric store values are rejected instead of silently disabling the cap.

[Unreleased]: https://github.com/llmbrake/llmbrake/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/llmbrake/llmbrake/releases/tag/v0.2.1
[0.1.0]: https://github.com/llmbrake/llmbrake/releases/tag/v0.1.0
