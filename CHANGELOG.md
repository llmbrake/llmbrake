# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/llmbrake/llmbrake/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/llmbrake/llmbrake/releases/tag/v0.1.0
