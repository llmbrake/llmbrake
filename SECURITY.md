# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report vulnerabilities privately through GitHub's private vulnerability reporting: go to the [Security tab](https://github.com/llmbrake/llmbrake/security) of this repository and choose **"Report a vulnerability"** ([direct link](https://github.com/llmbrake/llmbrake/security/advisories/new)).

Please include:

- the affected version(s)
- a description of the issue and its impact (for example, a way to bypass a budget cap)
- steps or a minimal proof of concept to reproduce it

We'll acknowledge your report, keep you updated while we investigate and fix it, and credit you in the advisory if you'd like.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Scope notes

`llmbrake` runs inside your application and does not read or store prompt or completion content. Spend totals live in the store you configure (in memory by default). Securing that store, your provider API keys and your application is up to you.
