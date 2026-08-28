---
hermes-mpp: patch
---

Delegated multi-challenge selection to pympp so Hermes prefers a funded Tempo charge currency while preserving server order when balances cannot be checked. Added `get_challenge_priority` support to `ChallengeTempo` and made the `_match` function async to support the new `select_challenge` runtime API. Updated pympp dependency to a git revision that includes the `select_challenge` and `get_challenge_priority` features.
