# Changelog

## 0.1.4 (2026-10-27)

### Patch Changes

- Require urllib3 2.8 or newer and upgrade the locked PyJWT and urllib3 dependencies to patched releases. (by @brendanjryan, [#55](https://github.com/tempoxyz/hermes-mpp/pull/55))
- Upgraded PyJWT to 2.15.0 and urllib3 to 2.8.0 to address vulnerable dependency versions, and added an explicit `urllib3>=2.8,<3` requirement to the package dependencies. (by @brendanjryan, [#55](https://github.com/tempoxyz/hermes-mpp/pull/55))
- Add native Hermes plugin packaging for reviewed Plugin Catalog installation.
- Allow discovery before wallet setup without instrumenting HTTPX or exposing the
- payment tool until a private key is configured. (by @brendanjryan, [#55](https://github.com/tempoxyz/hermes-mpp/pull/55))
- Updated `certifi` dependency to version `2026.10.19` and pinned it with an `exclude-newer-package` override to ensure a specific release is used. (by @brendanjryan, [#55](https://github.com/tempoxyz/hermes-mpp/pull/55))
- Upgraded Pillow from 12.3.0 to 13.0.0 to address security vulnerabilities. Updated dependency cooldown configuration to allow the Pillow security release through. (by @brendanjryan, [#55](https://github.com/tempoxyz/hermes-mpp/pull/55))
- Updated `aiohttp` dependency from version 3.14.3 to 3.14.4 in the lock file. (by @brendanjryan, [#55](https://github.com/tempoxyz/hermes-mpp/pull/55))
- Set package cooldown from 7 days to 1 day, reducing the minimum age required for dependencies to be included. (by @brendanjryan, [#55](https://github.com/tempoxyz/hermes-mpp/pull/55))
- Updated CI dependencies and improved security configurations. Bumped `actions/checkout` from v7.0.0 to v7.0.1 across all workflows, updated the dependabot reusable workflow pin, added hash verification to pip-audit, enforced locked environment for `uv run` commands, and added `exclude-newer = "7 days"` to `pyproject.toml` for reproducible dependency resolution. (by @brendanjryan, [#55](https://github.com/tempoxyz/hermes-mpp/pull/55))

## 0.1.3 (2026-08-28)

### Patch Changes

- Fixed wallet environment file writes to use `tempfile.mkstemp` so the temporary file is created with owner-only permissions (0o600) from the start, preventing a window where the private key could be readable by other users. (by @ParvAhuja, [#29](https://github.com/tempoxyz/hermes-mpp/pull/29))
- Required the pympp release that supports MACH as an ordinary Tempo charge currency. (by @ParvAhuja, [#29](https://github.com/tempoxyz/hermes-mpp/pull/29))

## 0.1.2 (2026-08-11)

### Patch Changes

- Updated the development lock to patched `cryptography` 50.0.0 and `pillow` 12.3.0 despite the older versions pinned by Hermes Agent 0.19. (by @ParvAhuja, [#22](https://github.com/tempoxyz/hermes-mpp/pull/22))
- Validate and persist payment origins during installation. (by @ParvAhuja, [#22](https://github.com/tempoxyz/hermes-mpp/pull/22))
- Simplify payment-attempt state while preserving existing payment behavior. (by @ParvAhuja, [#22](https://github.com/tempoxyz/hermes-mpp/pull/22))

## 0.1.1 (2026-08-04)

### Patch Changes

- Make the installer work from isolated environments and discover standard Hermes installations. (by @ParvAhuja, [#7](https://github.com/tempoxyz/hermes-mpp/pull/7))
