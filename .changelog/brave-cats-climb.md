---
hermes-mpp: patch
---

Fixed wallet environment file writes to use `tempfile.mkstemp` so the temporary file is created with owner-only permissions (0o600) from the start, preventing a window where the private key could be readable by other users.
