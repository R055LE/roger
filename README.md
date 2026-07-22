# roger

An [OpenRouter](https://openrouter.ai)-backed Discord bot — the spiritual successor to
[`roger-bot`](https://github.com/R055LE/roger-bot), my first-ever programming project. Same
character, rebuilt from scratch on hosted models and modern tooling.

> **Status:** scaffolding. The design spec is incoming — stack, structure, and features follow
> it. Everything here is deliberately minimal so the spec drives the real shape.

## Intent (provisional)

- A Discord chat bot (mentions, DMs, and configured channels) with a small, focused persona.
- OpenAI-compatible client pointed at OpenRouter, so the model is a config value, not a rewrite.
- Config via environment only — **no secrets in git**. See [`.env.example`](.env.example).

## Setup

```bash
cp .env.example .env   # then fill in the tokens
```

Full run/deploy instructions land with the spec.

## Security

- `.env` and any live persona/config are gitignored; commit `.env.example` with placeholders only.
- API keys (Discord, OpenRouter) are read from the environment at runtime — never hardcoded.

## License

MIT — see [LICENSE](LICENSE).
