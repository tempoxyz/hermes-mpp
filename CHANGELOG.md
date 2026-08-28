# Changelog

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
