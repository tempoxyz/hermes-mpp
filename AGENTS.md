# Hermes MPP

- Keep the Hermes registration layer small and tool-free.
- HTTPX-specific integration belongs in this repository, not in pympp core.
- Payments must fail closed outside `MPP_ALLOWED_ORIGINS`.
- Never log or commit private keys.
- Hermes plugin guide: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- pympp: https://github.com/tempoxyz/pympp
