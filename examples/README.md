# llmbrake examples

Small, tested integrations. Each folder has the helper you can copy into your app and a test that runs it against a mock or fake provider, so CI exercises the real library and the real framework code without API keys or network calls.

| Folder | Framework | How it hooks in |
|---|---|---|
| [`vercel-ai-sdk/`](vercel-ai-sdk/) | [Vercel AI SDK](https://ai-sdk.dev) (`ai@7`) | `guard.check()` before `generateText`/`streamText`, then charge from `result.usage` (or in `onFinish` for streams). Includes a Next.js route handler sketch. |
| [`openrouter/`](openrouter/) | [OpenRouter](https://openrouter.ai) via the `openai` SDK | `guard.wrap()` on `chat.completions.create` with OpenRouter model IDs in the price table. |
| [`langchain/`](langchain/) | [LangChain.js](https://js.langchain.com) chat models | `guard.check()` before `invoke()`, then charge from `AIMessage.usage_metadata`. |

All three map the framework's token usage (input, cached input, output) to llmbrake's cost function, so cached tokens are priced at the cached rate.

## Run the tests

Requires Node 22+ (the AI SDK 7 minimum). From the repo root:

```bash
npm ci            # builds llmbrake (prepare script)
cd examples
npm ci
npm test          # type-checks, then runs the tests with node:test
```

## Caveats (same as the library)

- Prices in `DEFAULT_PRICES` and the OpenRouter table were read on 2026-10-09. **Verify them** before relying on them.
- `MemoryStore` is per process. On serverless or multiple instances, use a shared store (see the Redis example in the main README).
- Concurrent calls for the same customer can overshoot the cap by roughly their own cost.
- If a stream is aborted before usage arrives, that call isn't charged.
