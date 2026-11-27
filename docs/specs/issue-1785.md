# Issue 1785: Remove stale CODER_STRONG_MODEL override

The `CODER_STRONG_MODEL` environment variable in `scripts/config/twt-queue.example.json` was a stale configuration override. Following the implementation of dynamic strong-model routing in PR #1753, this hardcoded pin is no longer necessary and contradicts the current routing contract.

## Changes
- Removed `"CODER_STRONG_MODEL": "route:strong"` from `scripts/config/twt-queue.example.json`.
- Added a test case `test_no_stale_strong_model_override` to `backend/tests/test_openrouter_contract.py` to ensure the environment remains clean of this stale override.
