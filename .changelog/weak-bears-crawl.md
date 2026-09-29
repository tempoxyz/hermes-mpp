---
hermes-mpp: patch
---

Updated CI dependencies and improved security configurations. Bumped `actions/checkout` from v7.0.0 to v7.0.1 across all workflows, updated the dependabot reusable workflow pin, added hash verification to pip-audit, enforced locked environment for `uv run` commands, and added `exclude-newer = "7 days"` to `pyproject.toml` for reproducible dependency resolution.
