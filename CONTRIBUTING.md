# Contributing to llmbrake

Thanks for helping. Bug reports, price-table corrections, docs fixes and pull requests are all welcome.

## Development

Requirements: Node 18 or newer and npm.

```bash
npm install
npm run build   # compiles src/ to dist/
npm test        # builds, compiles the tests, runs them with node:test
```

CI runs the same steps on Node 18, 20 and 22 for every push and pull request.

## Pull requests

- Keep changes focused; one topic per pull request.
- Add or update tests in `test/` for behavior changes.
- Keep the library free of runtime dependencies.
- Update `README.md` when options or behavior change, and add a line under "Unreleased" in `CHANGELOG.md`.
- Never log, store or transmit prompt or completion content. `llmbrake` handles usage metadata only.

## Price table changes

Prices in `src/prices.ts` must cite a public source. When you correct or add a model, include the provider's pricing page URL and the date you read it, both in the pull request and in the entry's `source` field.

## Reporting bugs

Open an issue with the `llmbrake` version, Node version, provider/SDK, a minimal reproduction, and what you expected to happen. For security problems, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
