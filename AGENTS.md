# Hermes MPP

- Keep the Hermes registration layer small: ambient HTTPX plus one generic fetch tool.
- HTTPX-specific integration belongs in this repository, not in pympp core.
- `MPP_ALLOWED_ORIGINS` restricts payments only when explicitly configured.
- Never log or commit private keys.
- Hermes plugin guide: https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin/
- pympp: https://github.com/tempoxyz/pympp
