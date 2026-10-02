---
hermes-mpp: patch
---

Updated the development dependency hermes-agent from v0.19.0 to v0.21.5, sourced as an editable Git submodule instead of a PyPI wheel, since upstream no longer supports wheel builds. Added submodule initialization to all CI workflow jobs and excluded the `.vendor` directory from linting and sdist builds.
